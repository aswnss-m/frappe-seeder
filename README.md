# Seeder

Seed an ERPNext company and an HRMS company with several years of realistic, reproducible demo data. Every record the seeder creates is tagged, so you can remove it later without touching anything else.

## Setup

1. Install the app on the site. ERPNext must be installed first; HRMS is needed for `seed-hrms`.

   ```
   bench --site <site> install-app seeder
   ```

2. Enable developer mode. The seeders refuse to run without it, unless you pass `--force`.

   ```
   bench --site <site> set-config developer_mode 1
   ```

3. Migrate, so the Seeder workspace and page are loaded.

   ```
   bench --site <site> migrate
   ```

## Seed ERPNext

```
bench --site <site> seed-erpnext --years 3 --records 2000
```

| Option | Default | What it does |
|---|---|---|
| `--years` | 3 | How many years of transactions to generate |
| `--records` | 2000 | Approximate number of documents to create |
| `--company` | default company | Company to seed (see below) |
| `--seed` | 42 | Random seed. The same seed gives the same data |
| `--modules` | all | Comma list of extra modules to seed (see below) |
| `--reset` | off | Remove what a previous ERPNext run created, then seed again |
| `--force` | off | Allow running without developer mode |

A 3-year, 2,000-record run takes about 4 minutes. Progress prints in the terminal as it goes.

### Modules

Sales, purchasing and journal entries are always seeded. `--modules` picks the extra areas:

| Module | What it creates |
|---|---|
| `buying` | Material requests, supplier quotations |
| `selling` | Quotations |
| `stock` | Stock receipts, transfers, issues, reconciliations |
| `manufacturing` | Finished goods, bills of materials, work orders |
| `quality` | Incoming quality inspections on purchase receipts |
| `assets` | Fixed assets with depreciation schedules |
| `projects` | Projects with tasks |
| `support` | Issues |
| `crm` | Leads and opportunities |

Examples:

```
bench --site <site> seed-erpnext --modules buying,selling,stock
bench --site <site> seed-erpnext --modules all
```

Payroll is not an ERPNext module here. The HRMS seeder creates the salary slips.

## Seed HRMS

```
bench --site <site> seed-hrms --employees 20 --years 3
```

| Option | Default | What it does |
|---|---|---|
| `--employees` | 20 | Employees to create |
| `--years` | 3 | Years of history |
| `--company` | default company | Company to seed |
| `--seed` | 42 | Random seed |
| `--reset` | off | Remove what a previous HRMS run created, then seed again |
| `--force` | off | Allow running without developer mode |

It creates departments, designations, leave types, a holiday list, salary components and a structure. For each employee it also creates:

- a user, with the password **`Password123`**, on the domain `<company>.example.com`
- a salary assignment for each year, with the CTC rising 3% a year
- leave allocations and approved leave
- daily attendance
- a salary slip for every month since they joined

Rerunning with the same `--seed` skips employees that already exist.

Salary is simplified: unpaid leave doesn't reduce pay, and income tax is a flat 10% of gross, not a tax slab.

## Which company is used

- `--company` uses that company. If it doesn't exist, the seeder stops with an error.
- Otherwise it uses the site's default company.
- Otherwise it uses the oldest company on the site.
- It creates "Seeder Demo Co" only when the site has no company at all.

Missing setup is created automatically: ERPNext's base data (fixtures, warehouse types), the company's default accounts and cost center, and a default company for the user running the seed.

## Reset

```
bench --site <site> seed-erpnext --reset --years 3 --records 2000
bench --site <site> seed-hrms --reset --employees 20 --years 3
```

`--reset` cancels and deletes only records tagged by the previous run of that app. Customers, items, departments and other masters stay. ERPNext and HRMS data are tagged separately, so resetting one leaves the other alone.

## Seed from the Desk

Open the **Seeder** workspace in the sidebar, or go to `/app/seeder`. Pick ERPNext or HRMS, set the sizes, and click **Run**. The job runs in the background, so keep `bench start` running. Only System Managers can use the page.

The Desk page always seeds all ERPNext modules. Use the command line to choose modules.

## Also available through `bench execute`

```
bench --site <site> execute seeder.erpnext.seed --kwargs '{"years": 3, "records": 2000}'
bench --site <site> execute seeder.hrms.seed --kwargs '{"employees": 20, "years": 3}'
```

## Notes and troubleshooting

- **Stock reposts.** Backdated stock entries queue ERPNext valuation reposts. ERPNext runs them only inside the time window set in Stock Settings. Quantities are correct straight away; valuations for backdated entries settle when the reposts run. The seeder reports how many are still queued at the end of a run.
- **"Unknown column" errors.** A site whose database is behind its ERPNext code shows errors like `Unknown column 'tabContact.is_billing_contact'`. Run `bench --site <site> migrate` first. If the error stays, the site's custom fields are missing; contact the maintainer, or run `erpnext.setup.install.create_address_and_contact_custom_fields` from `bench console`.
- **Conflicts.** Background stock jobs can make a record conflict with the seeder's write. The seeder retries the record with growing waits, so it normally recovers on its own. Records that still fail are counted in the summary, and the first five are printed.

## For the gist

Each seed file is one script:

- `seeder/erpnext/seed.py` is standalone. Only ERPNext and Frappe are needed.
- `seeder/hrms/seed.py` imports the company setup and the developer-mode check from `seeder/erpnext/seed.py`. Publish the two files together, or copy those helpers into the HRMS file.
