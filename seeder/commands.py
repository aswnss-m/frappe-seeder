import click
import frappe
from frappe.commands import pass_context


@click.command("seed-erpnext")
@click.option("--years", default=3, show_default=True, type=int, help="Years of transactions to generate")
@click.option("--records", default=2000, show_default=True, type=int, help="Approximate number of documents")
@click.option("--company", default=None, help="Company to seed. Defaults to the Global Defaults company")
@click.option("--seed", "seed_value", default=42, show_default=True, type=int, help="Random seed")
@click.option("--reset", is_flag=True, help="Remove previously seeded ERPNext documents first")
@click.option("--modules", default="all", show_default=True, help="Comma list: buying,selling,stock,manufacturing,quality,assets,projects,support,crm, or all")
@click.option("--force", is_flag=True, help="Allow running without developer_mode")
@pass_context
def seed_erpnext(context, years, records, company, seed_value, reset, modules, force):
	"""Seed an ERPNext company with several years of transactions."""
	from seeder.erpnext.seed import seed

	site = context.sites[0]
	frappe.init(site=site)
	frappe.connect()
	try:
		seed(years=years, records=records, company=company, seed=seed_value, reset=reset, force=force, modules=modules)
	finally:
		frappe.destroy()


@click.command("seed-hrms")
@click.option("--employees", default=20, show_default=True, type=int, help="Employees to create")
@click.option("--years", default=3, show_default=True, type=int, help="Years of history to generate")
@click.option("--company", default=None, help="Company to seed. Defaults to the Global Defaults company")
@click.option("--seed", "seed_value", default=42, show_default=True, type=int, help="Random seed")
@click.option("--reset", is_flag=True, help="Remove previously seeded HRMS records first")
@click.option("--force", is_flag=True, help="Allow running without developer_mode")
@pass_context
def seed_hrms(context, employees, years, company, seed_value, reset, force):
	"""Seed employees, users (password Password123), attendance, leave and salary slips."""
	from seeder.hrms.seed import seed

	site = context.sites[0]
	frappe.init(site=site)
	frappe.connect()
	try:
		seed(employees=employees, years=years, company=company, seed=seed_value, reset=reset, force=force)
	finally:
		frappe.destroy()


commands = [seed_erpnext, seed_hrms]
