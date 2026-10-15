"""Seed an HRMS company with employees, users, attendance, leave and payroll.

Creates employees with Users (default password: Password123), attendance, leave,
salary structure assignments and monthly salary slips over the last `years` years.

Salaries are generated from a CTC per employee that grows 3% each year. Salary slips
are produced by HRMS's own make_salary_slip, so payroll validation stays in the loop.

Needs the ERPNext seed module for company setup:

    bench --site <site> execute seeder.hrms.seed.seed --kwargs '{"employees": 20, "years": 3}'

Same seed value gives the same data. Every created record is tagged with a
`seeded:hrms:<run_id>` comment so `reset=True` can remove exactly what this run created.
"""

import math
import random
import re
import uuid
from datetime import date, datetime, time, timedelta

import frappe
from frappe.utils import add_days, flt, getdate, nowdate

FIRST_NAMES = [
	"Aarav", "Priya", "Liam", "Olivia", "Mateo", "Sofia", "Kenji", "Yuki", "Amara", "Kwame",
	"Lukas", "Emma", "Noah", "Ananya", "Ravi", "Fatima", "Omar", "Chloe", "Ethan", "Mei",
	"Diego", "Ingrid", "Tomas", "Hana", "Samuel", "Leila", "Arjun", "Elena", "Jonas", "Zara",
]
LAST_NAMES = [
	"Sharma", "Patel", "Smith", "Garcia", "Tanaka", "Okafor", "Muller", "Rossi", "Kim", "Nair",
	"Brown", "Hernandez", "Singh", "Khan", "Dubois", "Silva", "Andersen", "Costa", "Walsh", "Ito",
]
DEPARTMENTS = {
	"Engineering": ["Software Engineer", "Senior Software Engineer", "QA Engineer", "Engineering Manager"],
	"Sales": ["Sales Executive", "Account Manager", "Sales Manager"],
	"Marketing": ["Marketing Executive", "Content Writer", "Marketing Manager"],
	"Finance": ["Accountant", "Finance Analyst", "Finance Manager"],
	"Human Resources": ["HR Specialist", "Recruiter", "HR Manager"],
	"Operations": ["Operations Associate", "Logistics Coordinator", "Operations Manager"],
	"Customer Support": ["Support Agent", "Support Lead"],
}
HOLIDAYS = [(1, 1, "New Year"), (5, 1, "Labour Day"), (8, 15, "Independence Day"), (12, 25, "Christmas")]
LEAVE_TYPES = {"Casual Leave": 12, "Sick Leave": 8}
SALARY_COMPONENTS = [
	# name, abbr used when the component is created, type, formula template.
	# Formulas refer to components by their real abbreviation, looked up at run time because
	# HRMS already ships some of these components with their own abbreviations (e.g. Basic -> B).
	("Basic", "BASIC", "Earning", "base * 0.4"),
	("House Rent Allowance", "HRA", "Earning", "base * 0.2"),
	("Special Allowance", "SPECIAL", "Earning", "base * 0.4"),
	("Provident Fund", "PF", "Deduction", "{BASIC} * 0.12"),
	# Simplified flat effective rate, not a tax slab.
	("Income Tax", "TAX", "Deduction", "({BASIC} + {HRA} + {SPECIAL}) * 0.10"),
]
DEFAULT_PASSWORD = "Password123"
BASE_CTC_MEDIAN = 85000
ANNUAL_RAISE = 0.03
ABSENCE_RATE = 0.03


def seed(employees=20, years=3, company=None, seed=42, reset=False, force=False):
	"""Create `employees` employees and their history over the last `years` years.

	:param employees: number of employees to create
	:param years: how many years back the history spans
	:param company: company to seed. Defaults to the Global Defaults company.
	:param seed: random seed. The same seed gives the same data.
	:param reset: first remove records previously created by this script
	:param force: allow running on a site without developer_mode
	"""
	from seeder.erpnext.seed import _ensure_base, _guard

	_guard(force)
	if "hrms" not in frappe.get_installed_apps():
		frappe.throw("The hrms app is not installed on this site.")
	if reset:
		reset_seeded()
	company = _ensure_base(company)
	return HrmsSeeder(company, int(employees), int(years), seed).run()


def reset_seeded():
	"""Cancel and delete HRMS records created by this script. Masters are kept."""
	marks = frappe.get_all(
		"Comment",
		filters={"comment_type": "Info", "content": ["like", "seeded:hrms:%"]},
		fields=["name", "reference_doctype", "reference_name"],
		order_by="creation desc",
	)
	deleted = {}
	for mark in marks:
		doctype, name = mark.reference_doctype, mark.reference_name
		if frappe.db.exists(doctype, name):
			doc = frappe.get_doc(doctype, name)
			doc.flags.ignore_permissions = True
			if doc.docstatus == 1:
				doc.cancel()
			frappe.delete_doc(doctype, name, force=1, ignore_permissions=True)
			deleted[doctype] = deleted.get(doctype, 0) + 1
		frappe.delete_doc("Comment", mark.name, force=1, ignore_permissions=True)
		# Commit per record: one long transaction would hold the naming-series rows and deadlock.
		frappe.db.commit()
	print(f"Reset removed: {deleted}")
	return deleted


class HrmsSeeder:
	def __init__(self, company, employees, years, seed):
		self.company = company
		self.n_employees = employees
		self.years = years
		self.base_seed = seed
		self.rnd = random.Random(seed)
		self.run_id = uuid.uuid4().hex[:8]
		self.marker = f"seeded:hrms:{self.run_id}"
		self.today = getdate(add_days(nowdate(), -1))
		self.start = self.today - timedelta(days=365 * years)
		self.counts = {}
		self.failures = []
		self.domain = re.sub(r"[^a-z0-9]", "", company.lower()) + ".example.com"

	# ----- entry point -------------------------------------------------------

	def run(self):
		print(f"Seeding {self.company}: {self.n_employees} employees, {self.years} years", flush=True)
		self._setup_masters()
		created = 0
		for i in range(self.n_employees):
			# One generator per employee, so skipping an existing employee doesn't shift the rest.
			self.rnd = random.Random(f"{self.base_seed}:{i}")
			first = self.rnd.choice(FIRST_NAMES)
			last = self.rnd.choice(LAST_NAMES)
			email = f"{first}.{last}{i}@{self.domain}".lower()
			if frappe.db.exists("User", email):
				_progress((i + 1) / self.n_employees * 100, f"Skipping {email}: already seeded")
				continue
			try:
				self._employee_history(first, last, email)
				frappe.db.commit()
				created += 1
			except Exception as exc:
				frappe.db.rollback()
				self.failures.append(f"{email}: {exc}")
			_progress((i + 1) / self.n_employees * 100, f"Seeding HRMS employees ({i + 1}/{self.n_employees})")

		summary = {
			"run_id": self.run_id,
			"company": self.company,
			"employees_created": created,
			"counts": self.counts,
			"failures": len(self.failures),
		}
		print(summary)
		for msg in self.failures[:5]:
			print("  failed:", msg)
		return summary

	# ----- masters -----------------------------------------------------------

	def _setup_masters(self):
		# This company's root department if it has one, else any root (HRMS ships "All Departments").
		self.root_department = frappe.db.get_value(
			"Department", {"is_group": 1, "company": self.company}, "name"
		) or frappe.db.get_value("Department", {"is_group": 1}, "name")
		for name in DEPARTMENTS:
			if not frappe.db.exists("Department", {"department_name": name, "company": self.company}):
				self._create(
					"Department",
					{"department_name": name, "company": self.company, "parent_department": self.root_department},
					master=True,
				)
		for dept_designations in DEPARTMENTS.values():
			for name in dept_designations:
				if not frappe.db.exists("Designation", name):
					self._create("Designation", {"designation_name": name}, master=True)
		for gender in ("Male", "Female"):
			if not frappe.db.exists("Gender", gender):
				self._create("Gender", {"gender": gender}, master=True)
		for name, days in LEAVE_TYPES.items():
			if not frappe.db.exists("Leave Type", name):
				self._create("Leave Type", {"leave_type_name": name, "max_leaves_allowed": days}, master=True)
		for name, abbr, kind, _ in SALARY_COMPONENTS:
			if not frappe.db.exists("Salary Component", name):
				self._create(
					"Salary Component",
					{
						"salary_component": name,
						"salary_component_abbr": abbr,
						"type": kind,
						"depends_on_payment_days": 0,  # the formulas here are fixed monthly amounts
					},
					master=True,
				)
			else:
				frappe.db.set_value("Salary Component", name, "depends_on_payment_days", 0)
		self.holiday_list = self._holiday_list()
		self.structure = self._salary_structure()
		frappe.db.commit()

	def _holiday_list(self):
		name = f"Seeder Holidays {self.company}"
		if frappe.db.exists("Holiday List", name):
			return name
		holidays = []
		for year in range(self.start.year, self.today.year + 1):
			for month, day, label in HOLIDAYS:
				holidays.append({"holiday_date": date(year, month, day), "description": label})
		doc = frappe.get_doc(
			{
				"doctype": "Holiday List",
				"holiday_list_name": name,
				"from_date": date(self.start.year, 1, 1),
				"to_date": date(self.today.year, 12, 31),
				"weekly_off": "Sunday",
				"holidays": holidays,
			}
		)
		doc.insert(ignore_permissions=True)
		return doc.name

	def _salary_structure(self):
		name = f"Seeder Standard {self.company}"
		if frappe.db.exists("Salary Structure", name):
			return name
		abbr_of = {
			component: frappe.db.get_value("Salary Component", component, "salary_component_abbr")
			for component, *_ in SALARY_COMPONENTS
		}
		keys = {
			"BASIC": abbr_of["Basic"],
			"HRA": abbr_of["House Rent Allowance"],
			"SPECIAL": abbr_of["Special Allowance"],
		}
		earnings, deductions = [], []
		for component, _, kind, formula in SALARY_COMPONENTS:
			row = {
				"salary_component": component,
				"abbr": abbr_of[component],
				"amount_based_on_formula": 1,
				"formula": formula.format(**keys),
				"depends_on_payment_days": 0,
			}
			(earnings if kind == "Earning" else deductions).append(row)
		doc = frappe.get_doc(
			{
				"doctype": "Salary Structure",
				"name": name,
				"company": self.company,
				"currency": frappe.db.get_value("Company", self.company, "default_currency"),
				"is_active": "Yes",
				"payroll_frequency": "Monthly",
				"earnings": earnings,
				"deductions": deductions,
			}
		)
		doc.insert(ignore_permissions=True, set_name=name)
		doc.submit()
		self._mark(doc)
		return doc.name

	# ----- one employee's history -------------------------------------------

	def _employee_history(self, first, last, email):
		from frappe.utils.password import update_password
		from hrms.payroll.doctype.salary_structure.salary_structure import make_salary_slip

		joined = min(self.start + timedelta(days=self.rnd.randint(0, 365 * self.years)), self.today - timedelta(days=30))
		gender = self.rnd.choice(["Male", "Female"])
		department = self.rnd.choice(list(DEPARTMENTS))
		designation = self.rnd.choice(DEPARTMENTS[department])
		department_id = frappe.db.get_value(
			"Department", {"department_name": department, "company": self.company}, "name"
		)

		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": first,
				"last_name": last,
				"send_welcome_email": 0,
				"enabled": 1,
				"user_type": "System User",
			}
		)
		user.insert(ignore_permissions=True)
		roles = ["Employee", "HR User"] if department == "Human Resources" else ["Employee"]
		user.add_roles(*roles)
		# Set the password directly: Frappe rejects "Password123" as too common when it is set on save.
		update_password(email, DEFAULT_PASSWORD)
		self._mark(user)

		employee = self._create(
			"Employee",
			{
				"naming_series": "HR-EMP-",
				"first_name": first,
				"last_name": last,
				"gender": gender,
				"date_of_birth": self._birth_date(joined),
				"date_of_joining": joined,
				"status": "Active",
				"company": self.company,
				"department": department_id,
				"designation": designation,
				"user_id": email,
				"prefered_email": email,
				"holiday_list": self.holiday_list,
			},
		)

		annual_ctc = round(self.rnd.lognormvariate(math.log(BASE_CTC_MEDIAN), 0.35), -2)
		self._assignments(employee.name, joined, annual_ctc)
		leave_dates = self._leaves(employee.name, joined)
		self._attendance(employee.name, joined, leave_dates)
		self._salary_slips(employee.name, joined, make_salary_slip)

	def _assignments(self, employee, joined, annual_ctc):
		"""One Salary Structure Assignment per year, with base = monthly gross after yearly raises."""
		for year in range(joined.year, self.today.year + 1):
			effective = joined if year == joined.year else date(year, 4, 1)
			if effective > self.today:
				continue
			monthly = round(annual_ctc / 12 * (1 + ANNUAL_RAISE) ** (year - self.start.year), 2)
			self._create(
				"Salary Structure Assignment",
				{
					"employee": employee,
					"salary_structure": self.structure,
					"from_date": effective,
					"base": monthly,
					"company": self.company,
					"currency": frappe.db.get_value("Company", self.company, "default_currency"),
				},
				submit=True,
			)

	def _leaves(self, employee, joined):
		"""Leave allocations and approved leave applications. Returns the set of leave dates."""
		leave_dates = set()
		for year in range(max(joined.year, self.start.year), self.today.year + 1):
			for leave_type, days in LEAVE_TYPES.items():
				self._create(
					"Leave Allocation",
					{
						"naming_series": "HR-LAL-.YYYY.-",
						"employee": employee,
						"leave_type": leave_type,
						"from_date": date(year, 1, 1),
						"to_date": date(year, 12, 31),
						"new_leaves_allocated": days,
						"company": self.company,
					},
					submit=True,
				)
			for _ in range(self._poisson(1.8)):
				leave_type = self.rnd.choice(list(LEAVE_TYPES))
				start_day = date(year, self.rnd.randint(1, 12), self.rnd.randint(1, 28))
				length = self.rnd.randint(1, 3)
				days = [add_days(start_day, k) for k in range(length)]
				if start_day < joined or days[-1] > self.today:
					continue
				if any(d in leave_dates for d in days):
					continue
				self._savepoint_try(
					lambda: self._create(
						"Leave Application",
						{
							"naming_series": "HR-LAP-.YYYY.-",
							"employee": employee,
							"leave_type": leave_type,
							"from_date": days[0],
							"to_date": days[-1],
							"posting_date": add_days(days[0], -3),
							"status": "Approved",
							"company": self.company,
						},
						submit=True,
					)
				)
				leave_dates.update(days)
		return leave_dates

	def _attendance(self, employee, joined, leave_dates):
		"""Present or absent on every working day, with check-in and check-out times."""
		holidays = set(
			frappe.get_all(
				"Holiday",
				filters={"parent": self.holiday_list},
				pluck="holiday_date",
			)
		)
		existing = set(
			frappe.get_all(
				"Attendance",
				filters={"employee": employee, "docstatus": ["!=", 2]},
				pluck="attendance_date",
			)
		)
		day = max(joined, self.start)
		while day <= self.today:
			skip = day.weekday() == 6 or day in holidays or day in existing
			if not skip:
				if day in leave_dates:
					status = "On Leave"
					check_in = check_out = None
				elif self.rnd.random() < ABSENCE_RATE:
					status = "Absent"
					check_in = check_out = None
				else:
					status = "Present"
					check_in, check_out = self._times(day)
				values = {
					"naming_series": "HR-ATT-.YYYY.-",
					"employee": employee,
					"attendance_date": day,
					"status": status,
					"company": self.company,
				}
				if check_in:
					values.update(in_time=check_in, out_time=check_out)
				self._savepoint_try(lambda v=values: self._create("Attendance", v, submit=True))
			day = add_days(day, 1)

	def _salary_slips(self, employee, joined, make_salary_slip):
		"""Monthly slips for each full month since joining, computed by HRMS from the assignments."""
		month = date(max(joined, self.start).year, max(joined, self.start).month, 1)
		while True:
			month_end = add_days(add_days(month, 32).replace(day=1), -1)
			if month_end > self.today:
				break

			def make():
				slip = make_salary_slip(self.structure, employee=employee, posting_date=month_end)
				slip.insert(ignore_permissions=True)
				slip.submit()
				self._mark(slip)
				self.counts["Salary Slip"] = self.counts.get("Salary Slip", 0) + 1

			self._savepoint_try(make)
			month = add_days(month_end, 1)

	# ----- helpers -----------------------------------------------------------

	def _times(self, day):
		in_hour = self.rnd.gauss(9.5, 0.25)
		span = self.rnd.gauss(8.5, 0.4)
		check_in = datetime.combine(day, time()) + timedelta(hours=in_hour)
		check_out = check_in + timedelta(hours=span)
		return check_in, check_out

	def _birth_date(self, joined):
		age = self.rnd.randint(22, 55)
		return date(joined.year - age, self.rnd.randint(1, 12), self.rnd.randint(1, 28))

	def _poisson(self, lam):
		"""Knuth's method for a small Poisson draw."""
		threshold, k, p = math.exp(-lam), 0, 1.0
		while True:
			p *= self.rnd.random()
			if p <= threshold:
				return k
			k += 1

	def _savepoint_try(self, fn):
		"""Run one record's creation in a savepoint, so a single bad record doesn't abort the employee."""
		frappe.db.savepoint("seeder")
		try:
			fn()
		except Exception as exc:
			frappe.db.rollback(save_point="seeder")
			self.failures.append(str(exc)[:200])

	def _create(self, doctype, values, submit=False, master=False):
		"""Insert (and optionally submit) a record. Masters are shared setup data, so they are not tagged."""
		doc = frappe.get_doc({"doctype": doctype, **values})
		doc.flags.ignore_permissions = True
		doc.insert(ignore_permissions=True)
		if submit:
			doc.submit()
		if master:
			return doc
		self._mark(doc)
		self.counts[doctype] = self.counts.get(doctype, 0) + 1
		return doc

	def _mark(self, doc):
		frappe.get_doc(
			{
				"doctype": "Comment",
				"comment_type": "Info",
				"reference_doctype": doc.doctype,
				"reference_name": doc.name,
				"content": self.marker,
			}
		).insert(ignore_permissions=True)


def _progress(percent, title):
	# Print to the terminal as well: realtime progress only reaches a browser session.
	print(f"[{percent:5.1f}%] {title}", flush=True)
	try:
		frappe.publish_progress(percent=percent, title=title)
	except Exception:
		pass
