import frappe
from frappe.utils import cint


@frappe.whitelist()
def start_seed(kind, years=3, records=2000, employees=20, company=None, reset=False):
	"""Queue a seeding run from the Desk. Only System Managers can start one."""
	frappe.only_for("System Manager")
	if kind not in ("erpnext", "hrms"):
		frappe.throw("kind must be 'erpnext' or 'hrms'")

	params = {
		"kind": kind,
		"years": cint(years),
		"records": cint(records),
		"employees": cint(employees),
		"company": company or None,
		"reset": cint(reset),
		"user": frappe.session.user,
	}
	frappe.enqueue("seeder.api.run_seed", queue="long", timeout=14400, params=params)
	return {"queued": True}


def run_seed(params):
	"""Runs in the background worker. The seed functions do their own guard checks."""
	if params["kind"] == "erpnext":
		from seeder.erpnext.seed import seed

		seed(years=params["years"], records=params["records"], company=params["company"], reset=params["reset"])
	else:
		from seeder.hrms.seed import seed

		seed(employees=params["employees"], years=params["years"], company=params["company"], reset=params["reset"])
	frappe.publish_realtime(
		"msgprint", {"message": "Seeding finished", "indicator": "green"}, user=params["user"]
	)
