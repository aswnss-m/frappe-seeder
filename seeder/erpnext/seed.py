"""Seed an ERPNext company with several years of believable transactions.

Standalone on purpose: only frappe and erpnext are imported, so this file can be
copied into any app (or pasted into a gist) and run with

    bench --site <site> execute seeder.erpnext.seed --kwargs '{"years": 3, "records": 2000}'

Every transaction is created through ERPNext's own validation, submit and
make_* helpers, so the general ledger and stock ledger stay consistent.

Data is generated from a seeded random generator, so the same `seed` value
produces the same data. Every transaction is tagged `seeded:erpnext:<run_id>` so
`reset=True` can remove exactly what this script created.
"""

import math
import random
import time
import uuid
from datetime import date, timedelta

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
COMPANY_WORDS = [
	"Apex", "Nimbus", "Vertex", "Orion", "Crestline", "Harbor", "Summit", "Pinnacle", "Meridian",
	"Atlas", "Lumen", "Keystone", "Bluewave", "Ironwood", "Silverline", "Northstar", "Redwood", "Cobalt",
]
COMPANY_SUFFIX = [
	"Traders", "Industries", "Supplies", "Solutions", "Logistics", "Systems", "Enterprises",
	"Works", "Distributors", "Labs", "Trading Co", "Group",
]
PRODUCT_ADJ = [
	"Premium", "Classic", "Eco", "Pro", "Compact", "Heavy-Duty", "Smart", "Industrial",
	"Essential", "Deluxe", "Standard", "Advanced",
]
PRODUCT_NOUN = [
	"Widget", "Bracket", "Sensor", "Cable", "Filter", "Valve", "Gasket", "Panel", "Adapter",
	"Bearing", "Motor", "Switch", "Pump", "Connector", "Battery", "Hose", "Lamp", "Fastener",
]

N_CUSTOMERS = 120
N_SUPPLIERS = 30
N_ITEMS = 60
OPENING_QTY = 300


def seed(years=3, records=2000, company=None, seed=42, reset=False, force=False, modules="all"):
	"""Create about `records` ERPNext documents spread over the last `years` years.

	:param years: how many years back the transactions span
	:param records: approximate number of documents to create
	:param company: company to seed. Defaults to the Global Defaults company.
	:param seed: random seed. The same seed gives the same data.
	:param reset: first cancel and delete documents previously created by this script
	:param force: allow running on a site without developer_mode
	"""
	_guard(force)
	if reset:
		reset_seeded()
	company = _ensure_base(company)
	return ErpnextSeeder(company, int(years), int(records), seed, modules).run()


def reset_seeded():
	"""Cancel and delete transactions created by this script. Masters are kept.

	Each seeded document has an Info comment with its run marker. Walking those comments
	newest first unwinds links (payments before invoices, invoices before deliveries) in order.
	"""
	marks = frappe.get_all(
		"Comment",
		filters={"comment_type": "Info", "content": ["like", "seeded:erpnext:%"]},
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


MODULES = ("buying", "selling", "stock", "manufacturing", "quality", "assets", "projects", "support", "crm")
TASK_NAMES = ["Requirements", "Design", "Build", "Testing"]
SUPPORT_SUBJECTS = [
	"Cannot log in to the portal",
	"Invoice amount does not match the order",
	"Delivery is delayed",
	"Product arrived damaged",
	"Warranty question",
	"Request for a product manual",
]


class ModulesMixin:
	"""Records for buying, selling, stock, manufacturing, quality, assets, projects, support and CRM.

	Each module adds its own prerequisites (warehouses, BOMs, templates, categories) when they are
	missing, and every record goes through the same marker and per-record commit as the chains.
	"""

	def _run_modules(self):
		for name in self.modules:
			print(f"Seeding {name}...", flush=True)
			getattr(self, f"_module_{name}")()

	def _attempt(self, fn, label):
		self._retry(fn, label)

	def _retry(self, fn, label):
		"""Run one record and commit it.

		A conflict means ERPNext's background repost of stock valuation changed the same stock rows.
		Waiting and retrying lets the repost finish, then the record posts against the settled rows.
		"""
		for attempt in range(1, 8):
			try:
				fn()
				frappe.db.commit()
				return
			except Exception as exc:
				frappe.db.rollback()
				if attempt == 7 or not _is_conflict(exc):
					self.failures.append(f"{label}: {exc}")
					return
				time.sleep(min(2**attempt, 30))

	def _report_reposts(self):
		"""ERPNext runs stock reposts inside its configured time window. Report what is still queued."""
		queued = frappe.db.count("Repost Item Valuation", {"status": ["in", ["Queued", "In Progress"]]})
		if queued:
			print(
				f"{queued} stock valuation reposts are queued. ERPNext runs them in the Stock Settings time window.",
				flush=True,
			)

	def _count(self, share):
		return max(1, int(self.records * share))

	def _random_day(self):
		span = (self.today - self.start).days
		day = self.start + timedelta(days=self.rnd.randint(0, span))
		return day if day.weekday() != 6 else day - timedelta(days=1)

	def _new(self, doctype, values):
		return frappe.get_doc({"doctype": doctype, **values})

	def _insert(self, doc):
		"""Insert a non-submittable record, tagged like the submitted ones."""
		doc.flags.ignore_permissions = True
		doc.insert(ignore_permissions=True)
		self._mark(doc)
		self.counts[doc.doctype] = self.counts.get(doc.doctype, 0) + 1
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

	def _account(self, account_type):
		return frappe.db.get_value(
			"Account", {"company": self.company, "account_type": account_type, "is_group": 0}, "name"
		)

	def _warehouse(self, name):
		found = frappe.db.get_value("Warehouse", {"warehouse_name": name, "company": self.company}, "name")
		if found:
			# Warehouses created by an earlier run may lack the stock account that postings need.
			if not frappe.db.get_value("Warehouse", found, "account"):
				frappe.db.set_value("Warehouse", found, "account", self._account("Stock"))
				frappe.db.commit()
				frappe.clear_document_cache("Warehouse", found)
				frappe.flags.pop("warehouse_account_map", None)
			return found
		root = frappe.db.get_value("Warehouse", {"company": self.company, "is_group": 1}, "name")
		warehouse = self._insert_master(
			{
				"doctype": "Warehouse",
				"warehouse_name": name,
				"company": self.company,
				"parent_warehouse": root,
				"account": self._account("Stock"),
			}
		)
		# ERPNext keeps a warehouse-to-account map for the life of the process. A map built before
		# this warehouse existed makes stock postings fail as "not linked to any account".
		frappe.clear_document_cache("Warehouse", warehouse.name)
		frappe.flags.pop("warehouse_account_map", None)
		return warehouse.name

	def _insert_master(self, values):
		# Commit masters at once: a later record's rollback must not undo a warehouse or item others depend on.
		doc = frappe.get_doc(values)
		doc.flags.ignore_permissions = True
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
		return doc

	def _item(self, code, name, **extra):
		if not frappe.db.exists("Item", code):
			self._insert_master(
				{
					"doctype": "Item",
					"item_code": code,
					"item_name": name,
					"item_group": self.item_group,
					"stock_uom": "Nos",
					**extra,
				}
			)
		return code

	# ----- buying --------------------------------------------------------------

	def _module_buying(self):
		for _ in range(self._count(0.02)):
			self._attempt(lambda d=self._random_day(): self._material_request(d), "material request")
		for _ in range(self._count(0.02)):
			self._attempt(lambda d=self._random_day(): self._supplier_quotation(d), "supplier quotation")

	def _material_request(self, day):
		lines = self._lines(self.items, "buy", qty_range=(10, 40))
		mr = self._new(
			"Material Request",
			{
				"material_request_type": "Purchase",
				"company": self.company,
				"transaction_date": day,
				"schedule_date": add_days(day, 14),
				"items": [
					{"item_code": c, "qty": q, "schedule_date": add_days(day, 14), "warehouse": self.warehouse}
					for c, q, _ in lines
				],
			},
		)
		self._submit(mr)

	def _supplier_quotation(self, day):
		lines = self._lines(self.items, "buy")
		sq = self._build(
			"Supplier Quotation",
			{
				"supplier": self.rnd.choice(self.suppliers),
				"transaction_date": day,
				"valid_till": add_days(day, 30),
				"status": "Submitted",
				"items": [
					{"item_code": c, "qty": q, "rate": r, "warehouse": self.warehouse} for c, q, r in lines
				],
			},
		)
		self._submit(sq)

	# ----- selling -------------------------------------------------------------

	def _module_selling(self):
		for _ in range(self._count(0.03)):
			self._attempt(lambda d=self._random_day(): self._quotation(d), "quotation")

	def _quotation(self, day):
		customer = self.rnd.choices(self.customers, weights=self.customer_w)[0]
		lines = self._lines(self.items, "sell")
		quotation = self._build(
			"Quotation",
			{
				"quotation_to": "Customer",
				"party_name": customer,
				"order_type": "Sales",
				"transaction_date": day,
				"valid_till": add_days(day, 30),
				"selling_price_list": "Standard Selling",
				"price_list_currency": self.currency,
				"plc_conversion_rate": 1,
				"status": "Draft",
				"items": [{"item_code": c, "qty": q, "rate": r} for c, q, r in lines],
			},
		)
		self._submit(quotation)

	# ----- stock ---------------------------------------------------------------

	def _module_stock(self):
		self.store = self._warehouse("Retail Store")
		kinds = ["receipt", "transfer", "issue"]
		for _ in range(self._count(0.05)):
			kind = self.rnd.choices(kinds, weights=[4, 3, 3])[0]
			self._attempt(lambda k=kind, d=self._random_day(): getattr(self, f"_stock_{k}")(d), f"stock {kind}")
		for year in range(self.start.year, self.today.year + 1):
			for quarter in range(4):
				day = date(year, 1 + 3 * quarter, 15)
				if self.start <= day <= self.today:
					self._attempt(lambda d=day: self._stock_reconcile(d), "stock reconciliation")

	def _stock_receipt(self, day):
		item = self.rnd.choice(self.items)
		se = self._new(
			"Stock Entry",
			{
				"stock_entry_type": "Material Receipt",
				"company": self.company,
				"items": [
					{
						"item_code": item["code"],
						"qty": self.rnd.randint(20, 100),
						"t_warehouse": self.warehouse,
						"basic_rate": item["buy"],
					}
				],
			},
		)
		self._post(se, day)

	def _stock_transfer(self, day):
		item = self.rnd.choice(self.items)
		se = self._new(
			"Stock Entry",
			{
				"stock_entry_type": "Material Transfer",
				"company": self.company,
				"items": [
					{
						"item_code": item["code"],
						"qty": self.rnd.randint(5, 30),
						"s_warehouse": self.warehouse,
						"t_warehouse": self.store,
					}
				],
			},
		)
		self._post(se, day)

	def _stock_issue(self, day):
		item = self.rnd.choice(self.items)
		se = self._new(
			"Stock Entry",
			{
				"stock_entry_type": "Material Issue",
				"company": self.company,
				"items": [
					{"item_code": item["code"], "qty": self.rnd.randint(1, 3), "s_warehouse": self.warehouse}
				],
			},
		)
		self._post(se, day)

	def _stock_reconcile(self, day):
		rows = []
		for item in self.rnd.sample(self.items, 5):
			current = frappe.db.get_value(
				"Bin", {"item_code": item["code"], "warehouse": self.warehouse}, "actual_qty"
			) or 0
			rows.append(
				{
					"item_code": item["code"],
					"warehouse": self.warehouse,
					"qty": max(0, round(current * self.rnd.uniform(0.9, 1.1))),
					"valuation_rate": item["buy"],
				}
			)
		sr = self._new(
			"Stock Reconciliation",
			{
				"purpose": "Stock Reconciliation",
				"company": self.company,
				"posting_time": "12:00:00",
				"items": rows,
			},
		)
		self._post(sr, day)

	# ----- manufacturing -------------------------------------------------------

	def _module_manufacturing(self):
		self.finished = []
		self.boms = {}
		for i in range(1, 6):
			code = self._item(
				f"SEED-FG-{i:02d}",
				f"{self.rnd.choice(PRODUCT_ADJ)} Assembly {i}",
				is_stock_item=1,
				maintain_stock=1,
				is_sales_item=1,
			)
			self.finished.append(code)
		for code in self.finished:
			self._attempt(lambda c=code: self._bom(c), "bom")
		self.wip = frappe.db.get_value(
			"Warehouse", {"company": self.company, "warehouse_name": "Work In Progress"}, "name"
		)
		if not self.boms:
			return
		for _ in range(max(2, self._count(0.03) // 3)):
			self._attempt(lambda d=self._random_day(): self._work_order(d), "work order")

	def _bom(self, code):
		raws = self.rnd.sample(self.items, self.rnd.randint(2, 4))
		# BOM has its own company_currency() method, so it cannot take the attribute _build sets.
		bom = self._new(
			"BOM",
			{
				"item": code,
				"company": self.company,
				"currency": self.currency,
				"conversion_rate": 1,
				"quantity": 1,
				"items": [{"item_code": r["code"], "qty": self.rnd.randint(1, 4), "rate": r["buy"]} for r in raws],
			},
		)
		self._submit(bom)
		self.boms[code] = bom.name

	def _work_order(self, day):
		from erpnext.manufacturing.doctype.work_order.work_order import make_stock_entry

		code = self.rnd.choice(self.finished)
		qty = self.rnd.randint(5, 40)
		wo = self._new(
			"Work Order",
			{
				"production_item": code,
				"bom_no": self.boms[code],
				"qty": qty,
				"company": self.company,
				"source_warehouse": self.warehouse,
				"fg_warehouse": self.warehouse,
				"wip_warehouse": self.wip,
				"planned_start_date": day,
				"status": "Not Started",
			},
		)
		self._submit(wo)
		# make_stock_entry returns a dict of the new document, not a document
		self._post(frappe.get_doc(make_stock_entry(wo.name, "Material Transfer for Manufacture", qty)), add_days(day, 1))
		self._post(frappe.get_doc(make_stock_entry(wo.name, "Manufacture", qty)), add_days(day, 2))

	# ----- quality -------------------------------------------------------------

	def _module_quality(self):
		if not self.receipts:
			return
		# Inspections are only allowed on items flagged as requiring them, so the flag is on
		# only while this module runs. Left on, it would block the next run's opening stock.
		self._set_inspection_flag(1)
		try:
			self._quality_records()
		finally:
			self._set_inspection_flag(0)

	def _set_inspection_flag(self, value):
		for item in self.items:
			frappe.db.set_value("Item", item["code"], "inspection_required_before_purchase", value)
		frappe.db.commit()

	def _quality_records(self):
		self.qi_template = "Seeder Incoming Check"
		if not frappe.db.exists("Quality Inspection Template", self.qi_template):
			self._insert_master(
				{
					"doctype": "Quality Inspection Template",
					"quality_inspection_template_name": self.qi_template,
					"item_quality_inspection_parameter": [
						{"specification": "Weight (kg)", "numeric": 1, "min_value": 9.5, "max_value": 10.5},
						{"specification": "Length (cm)", "numeric": 1, "min_value": 99, "max_value": 101},
					],
				}
			)
		for _ in range(self._count(0.02)):
			self._attempt(lambda: self._quality_inspection(), "quality inspection")

	def _quality_inspection(self):
		receipt, item_code, day = self.rnd.choice(self.receipts)
		specs = frappe.get_all(
			"Item Quality Inspection Parameter",
			filters={"parent": self.qi_template},
			fields=["specification", "min_value", "max_value"],
		)
		readings, accepted = [], True
		for spec in specs:
			value = round(self.rnd.gauss((spec.min_value + spec.max_value) / 2, (spec.max_value - spec.min_value) / 5), 2)
			ok = spec.min_value <= value <= spec.max_value
			accepted = accepted and ok
			readings.append(
				{
					"specification": spec.specification,
					"numeric": 1,
					"min_value": spec.min_value,
					"max_value": spec.max_value,
					"reading_1": str(value),
					"status": "Accepted" if ok else "Rejected",
				}
			)
		qi = self._new(
			"Quality Inspection",
			{
				"inspection_type": "Incoming",
				"reference_type": "Purchase Receipt",
				"reference_name": receipt,
				"item_code": item_code,
				"report_date": day,
				"sample_size": 1,
				"inspected_by": "Administrator",
				"quality_inspection_template": self.qi_template,
				"status": "Accepted" if accepted else "Rejected",
				"readings": readings,
			},
		)
		self._submit(qi)

	# ----- assets --------------------------------------------------------------

	def _module_assets(self):
		category = self._asset_category()
		location = self._warehouse_free_location()
		for i in range(1, self.years * 4 + 1):
			code = self._item(
				f"SEED-AST-{i:03d}",
				f"Office Equipment {i}",
				is_fixed_asset=1,
				is_stock_item=0,
				asset_category=category,
			)
			day = self._random_day()
			value = round(self.rnd.lognormvariate(8.5, 0.6), 2)
			self._attempt(lambda c=code, d=day, v=value: self._asset(c, d, v, category, location), "asset")

	def _asset_category(self):
		name = "Seeder Office Equipment"
		if not frappe.db.exists("Asset Category", name):
			self._insert_master(
				{
					"doctype": "Asset Category",
					"asset_category_name": name,
					"depreciation_method": "Straight Line",
					"total_number_of_depreciations": 36,
					"frequency_of_depreciation": 12,
					"accounts": [
						{
							"company_name": self.company,
							"fixed_asset_account": self._account("Fixed Asset"),
							"accumulated_depreciation_account": self._account("Accumulated Depreciation"),
							"depreciation_expense_account": self._account("Depreciation"),
						}
					],
				}
			)
		return name

	def _warehouse_free_location(self):
		name = "Head Office"
		if not frappe.db.exists("Location", name):
			self._insert_master({"doctype": "Location", "location_name": name})
		return name

	def _asset(self, code, day, value, category, location):
		# Depreciation starts the month after the asset is available for use.
		in_use = add_days(day, 3)
		first_of_next_month = date(in_use.year + (in_use.month == 12), in_use.month % 12 + 1, 1)
		asset = self._new(
			"Asset",
			{
				"item_code": code,
				"asset_name": f"Office Equipment {code[-3:]} {day:%Y%m}",
				"asset_category": category,
				"company": self.company,
				"location": location,
				"purchase_date": day,
				"available_for_use_date": in_use,
				"gross_purchase_amount": value,
				"calculate_depreciation": 1,
				"finance_books": [
					{
						"depreciation_method": "Straight Line",
						"total_number_of_depreciations": 36,
						"frequency_of_depreciation": 12,
						"depreciation_start_date": first_of_next_month,
					}
				],
			},
		)
		self._submit(asset)

	# ----- projects ------------------------------------------------------------

	def _module_projects(self):
		for _ in range(self.years * 4):
			self._attempt(lambda d=self._random_day(): self._project(d), "project")

	def _project(self, day):
		completed = day < add_days(self.today, -120)
		project = self._new(
			"Project",
			{
				"project_name": f"{self.rnd.choice(PRODUCT_ADJ)} {self.rnd.choice(PRODUCT_NOUN)} rollout {day:%Y%m%d}",
				"company": self.company,
				"customer": self.rnd.choice(self.customers),
				"expected_start_date": day,
				"expected_end_date": add_days(day, 90),
				"status": "Completed" if completed else "Open",
			},
		)
		self._insert(project)
		for k, name in enumerate(TASK_NAMES):
			task = self._new(
				"Task",
				{
					"subject": name,
					"project": project.name,
					"status": "Completed" if completed else "Open",
					"exp_start_date": add_days(day, k * 20),
					"exp_end_date": add_days(day, k * 20 + 14),
				},
			)
			self._insert(task)

	# ----- support -------------------------------------------------------------

	def _module_support(self):
		for _ in range(self._count(0.03)):
			self._attempt(lambda d=self._random_day(): self._issue(d), "issue")

	def _issue(self, day):
		closed = self.rnd.random() < 0.7
		issue = self._new(
			"Issue",
			{
				"subject": self.rnd.choice(SUPPORT_SUBJECTS),
				"raised_by": f"support{self.rnd.randint(1, 9999)}@customers.example.com",
				"customer": self.rnd.choices(self.customers, weights=self.customer_w)[0],
				"priority": self.rnd.choice(["Low", "Medium", "High"]),
				"status": "Closed" if closed else "Open",
				"opening_date": day,
			},
		)
		self._insert(issue)

	# ----- CRM -----------------------------------------------------------------

	def _module_crm(self):
		self.leads = []
		sources = frappe.get_all("Lead Source", pluck="name")
		for _ in range(self._count(0.03)):
			self._attempt(lambda d=self._random_day(): self._lead(d, sources), "lead")
		for _ in range(self._count(0.02)):
			if self.leads:
				self._attempt(lambda d=self._random_day(): self._opportunity(d), "opportunity")

	def _lead(self, day, sources):
		name = self._person_or_company()
		lead = self._new(
			"Lead",
			{
				"lead_name": name,
				"company_name": name,
				"email_id": f"lead{self.rnd.randint(1, 99999)}@leads.example.com",
				"status": self.rnd.choice(["Lead", "Open", "Interested", "Converted", "Lost Quotation"]),
				"source": self.rnd.choice(sources) if sources else None,
			},
		)
		self._insert(lead)
		self.leads.append(lead.name)

	def _opportunity(self, day):
		opportunity = self._new(
			"Opportunity",
			{
				"opportunity_from": "Lead",
				"party_name": self.rnd.choice(self.leads),
				"opportunity_type": "Sales",
				"status": self.rnd.choice(["Open", "Quotation", "Converted", "Lost"]),
				"transaction_date": day,
				"expected_closing": add_days(day, 30),
				"opportunity_amount": round(self.rnd.lognormvariate(9, 0.7), 2),
				"company": self.company,
				"currency": self.currency,
			},
		)
		self._insert(opportunity)


class ErpnextSeeder(ModulesMixin):
	def __init__(self, company, years, records, seed, modules="all"):
		self.company = company
		self.years = years
		self.records = records
		self.rnd = random.Random(seed)
		self.run_id = uuid.uuid4().hex[:8]
		self.marker = f"seeded:erpnext:{self.run_id}"
		self.today = getdate(add_days(nowdate(), -1))
		self.start = self.today - timedelta(days=365 * years)
		self.counts = {}
		self.receipts = []
		self.modules = _parse_modules(modules)
		self.failures = []

	# ----- entry point -------------------------------------------------------

	def run(self):
		print(f"Seeding {self.company}: {self.years} years, about {self.records} records", flush=True)
		self._ensure_fiscal_years()
		self._load_defaults()
		print("Creating customers, suppliers and items...", flush=True)
		self._create_masters()
		tasks = self._plan_tasks()
		print(f"Creating {len(tasks)} chains of transactions...", flush=True)
		self._opening_stock()

		total = len(tasks)
		for i, (day, kind) in enumerate(tasks, start=1):
			self._retry(lambda k=kind, d=day: getattr(self, f"_{k}")(d), f"{kind} {day}")
			if i % 20 == 0 or i == total:
				_progress(i / total * 100, f"Seeding ERPNext transactions ({i}/{total})")

		self._run_modules()
		self._report_reposts()
		summary = {"run_id": self.run_id, "company": self.company, "counts": self.counts, "failures": len(self.failures)}
		print(summary)
		for msg in self.failures[:5]:
			print("  failed:", msg)
		return summary

	# ----- setup -------------------------------------------------------------

	def _ensure_fiscal_years(self):
		"""Create fiscal years for every stretch of the range that no existing fiscal year covers.

		Works whatever the existing year looks like: calendar years, April-to-March years, or none.
		"""
		for year in range(self.start.year, self.today.year + 1):
			for start, end in _uncovered_ranges(date(year, 1, 1), date(year, 12, 31)):
				frappe.get_doc(
					{
						"doctype": "Fiscal Year",
						"year": f"Seeder {start.isoformat()}",
						"year_start_date": start,
						"year_end_date": end,
						"companies": [{"company": self.company}],
					}
				).insert(ignore_permissions=True)

	def _load_defaults(self):
		self.cost_center = _ensure_cost_center(self.company)
		self.currency = frappe.db.get_value("Company", self.company, "default_currency")
		self.cash_account = frappe.db.get_value(
			"Account", {"company": self.company, "account_type": "Cash", "is_group": 0}, "name"
		)
		self.expense_account = frappe.db.get_value(
			"Account",
			{"company": self.company, "root_type": "Expense", "is_group": 0, "account_type": ""},
			"name",
		)
		self.warehouse = frappe.db.get_value("Warehouse", {"company": self.company, "is_group": 0}, "name")
		if not self.warehouse:
			parent = frappe.db.get_value("Warehouse", {"company": self.company, "is_group": 1}, "name")
			self.warehouse = frappe.get_doc(
				{"doctype": "Warehouse", "warehouse_name": "Main Warehouse", "company": self.company, "parent_warehouse": parent}
			).insert(ignore_permissions=True).name
		self.customer_group = _first_leaf("Customer Group")
		self.supplier_group = _first_leaf("Supplier Group")
		self.territory = _first_leaf("Territory")
		self.item_group = self._get_or_create_item_group()
		if not (self.cash_account and self.expense_account and self.cost_center):
			frappe.throw("Company needs a cash account, an expense account and a cost center to seed journals.")

	def _get_or_create_item_group(self):
		name = "Seeder Products"
		if not frappe.db.exists("Item Group", name):
			frappe.get_doc(
				{"doctype": "Item Group", "item_group_name": name, "parent_item_group": "All Item Groups"}
			).insert(ignore_permissions=True)
		return name

	def _create_masters(self):
		self.customers = []
		self.customer_w = []
		seen = set()
		while len(self.customers) < N_CUSTOMERS:
			name = self._person_or_company()
			if name in seen:
				continue
			seen.add(name)
			is_company = self.rnd.random() < 0.6
			if not frappe.db.exists("Customer", name):
				frappe.get_doc(
					{
						"doctype": "Customer",
						"customer_name": name,
						"customer_type": "Company" if is_company else "Individual",
						"customer_group": self.customer_group,
						"territory": self.territory,
					}
				).insert(ignore_permissions=True)
			self.customers.append(name)
		# Pareto-style weights: a few customers order far more often than the rest.
		self.rnd.shuffle(self.customers)
		self.customer_w = [1 / (rank + 1) ** 1.1 for rank in range(len(self.customers))]

		self.suppliers = []
		while len(self.suppliers) < N_SUPPLIERS:
			name = self._company_name()
			if name in self.suppliers:
				continue
			if not frappe.db.exists("Supplier", name):
				frappe.get_doc(
					{"doctype": "Supplier", "supplier_name": name, "supplier_type": "Company", "supplier_group": self.supplier_group}
				).insert(ignore_permissions=True)
			self.suppliers.append(name)

		self.items = []
		for i in range(1, N_ITEMS + 1):
			code = f"SEED-{i:04d}"
			buy = round(self.rnd.lognormvariate(4.5, 0.9), 2)
			sell = round(buy * self.rnd.uniform(1.25, 1.6), 2)
			item = {"code": code, "buy": buy, "sell": sell}
			if not frappe.db.exists("Item", code):
				name = f"{self.rnd.choice(PRODUCT_ADJ)} {self.rnd.choice(PRODUCT_NOUN)} {i}"
				frappe.get_doc(
					{
						"doctype": "Item",
						"item_code": code,
						"item_name": name,
						"item_group": self.item_group,
						"stock_uom": "Nos",
						"is_stock_item": 1,
						"maintain_stock": 1,
						"is_sales_item": 1,
						"is_purchase_item": 1,
					}
				).insert(ignore_permissions=True)
				_set_price("Standard Buying", code, buy)
				_set_price("Standard Selling", code, sell)
			self.items.append(item)
		frappe.db.commit()

	def _opening_stock(self):
		"""One receipt on the first day so early sales have stock to draw on."""
		pr = self._build(
			"Purchase Receipt",
			{
				"supplier": self.suppliers[0],
				"company": self.company,
				"posting_date": self.start,
				"set_posting_time": 1,
				"items": [
					{"item_code": it["code"], "qty": OPENING_QTY, "rate": it["buy"], "warehouse": self.warehouse}
					for it in self.items
				],
			},
		)
		self._submit(pr)
		frappe.db.commit()

	def _plan_tasks(self):
		"""Choose dates for each chain, weighted toward business days with growth and seasonality."""
		days, weights = [], []
		day = self.start
		while day <= self.today:
			if day.weekday() != 6:  # skip Sundays
				years_in = (day - self.start).days / 365
				season = 1 + 0.25 * _sin_month(day.month) + (0.4 if day.month in (11, 12) else 0)
				days.append(day)
				weights.append((1.1**years_in) * season)
			day += timedelta(days=1)

		# Each chain is about 4 documents. Split the record budget roughly by mix.
		n_sales = max(1, int(self.records * 0.40 / 4))
		n_purchase = max(1, int(self.records * 0.30 / 4))
		n_journal = max(1, int(self.records * 0.05))
		tasks = []
		for kind, n in (("sale", n_sales), ("purchase", n_purchase), ("journal", n_journal)):
			for day in self.rnd.choices(days, weights=weights, k=n):
				tasks.append((day, kind))
		tasks.sort(key=lambda t: t[0])
		return tasks

	# ----- chains ------------------------------------------------------------

	def _sale(self, day):
		from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note
		from erpnext.stock.doctype.delivery_note.delivery_note import make_sales_invoice
		from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

		customer = self.rnd.choices(self.customers, weights=self.customer_w)[0]
		lines = self._lines(self.items, "sell")
		so = self._build(
			"Sales Order",
			{
				"customer": customer,
				"company": self.company,
				"transaction_date": day,
				"delivery_date": add_days(day, 7),
				"items": [
					{"item_code": c, "qty": q, "rate": r, "delivery_date": add_days(day, 7), "warehouse": self.warehouse}
					for c, q, r in lines
				],
			},
		)
		self._submit(so)

		dn_day = self._clamp(add_days(day, self.rnd.randint(0, 3)))
		dn = make_delivery_note(so.name)
		self._post(dn, dn_day)

		si_day = self._clamp(add_days(dn_day, self.rnd.randint(0, 2)))
		si = make_sales_invoice(dn.name)
		self._post(si, si_day)

		if self.rnd.random() < 0.9:  # most invoices get paid, some are still open
			pe = get_payment_entry("Sales Invoice", si.name)
			frac = 1.0 if self.rnd.random() < 0.8 else round(self.rnd.uniform(0.3, 0.9), 2)
			amount = flt(si.grand_total * frac, 2)
			pe.paid_amount = amount
			pe.received_amount = amount
			for ref in pe.references:
				ref.allocated_amount = amount
			self._post(pe, self._clamp(add_days(si_day, self.rnd.randint(0, 45))), field="posting_date")

	def _purchase(self, day):
		from erpnext.buying.doctype.purchase_order.purchase_order import make_purchase_receipt
		from erpnext.stock.doctype.purchase_receipt.purchase_receipt import make_purchase_invoice
		from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

		supplier = self.rnd.choice(self.suppliers)
		lines = self._lines(self.items, "buy", qty_range=(50, 250))
		po = self._build(
			"Purchase Order",
			{
				"supplier": supplier,
				"company": self.company,
				"transaction_date": day,
				"schedule_date": add_days(day, 7),
				"items": [
					{"item_code": c, "qty": q, "rate": r, "schedule_date": add_days(day, 7), "warehouse": self.warehouse}
					for c, q, r in lines
				],
			},
		)
		self._submit(po)

		pr_day = self._clamp(add_days(day, self.rnd.randint(2, 7)))
		pr = make_purchase_receipt(po.name)
		self._post(pr, pr_day)
		self.receipts.append((pr.name, pr.items[0].item_code, pr_day))

		pi_day = self._clamp(add_days(pr_day, self.rnd.randint(0, 5)))
		pi = make_purchase_invoice(pr.name)
		self._post(pi, pi_day)

		if self.rnd.random() < 0.9:
			pe = get_payment_entry("Purchase Invoice", pi.name)
			self._post(pe, self._clamp(add_days(pi_day, self.rnd.randint(0, 30))), field="posting_date")

	def _journal(self, day):
		amount = round(self.rnd.lognormvariate(7.5, 0.8), 2)
		je = frappe.get_doc(
			{
				"doctype": "Journal Entry",
				"voucher_type": "Journal Entry",
				"company": self.company,
				"posting_date": day,
				"user_remark": f"{self.marker} operating expense",
				"accounts": [
					{"account": self.expense_account, "cost_center": self.cost_center, "debit_in_account_currency": amount},
					{"account": self.cash_account, "cost_center": self.cost_center, "credit_in_account_currency": amount},
				],
			}
		)
		self._submit(je)

	# ----- helpers -----------------------------------------------------------

	def _lines(self, items, price_key, qty_range=(1, 12)):
		"""Pick 1 to 4 distinct items with lognormal-ish quantities and noisy prices."""
		k = self.rnd.randint(1, 4)
		picked = self.rnd.sample(items, k)
		lines = []
		for it in picked:
			qty = min(qty_range[1] * 10, max(qty_range[0], int(self.rnd.lognormvariate(1.2, 0.6))))
			rate = round(it[price_key] * self.rnd.uniform(0.95, 1.08), 2)
			lines.append((it["code"], qty, rate))
		return lines

	def _person_or_company(self):
		if self.rnd.random() < 0.6:
			return self._company_name()
		return f"{self.rnd.choice(FIRST_NAMES)} {self.rnd.choice(LAST_NAMES)}"

	def _company_name(self):
		return f"{self.rnd.choice(COMPANY_WORDS)} {self.rnd.choice(COMPANY_SUFFIX)}"

	def _clamp(self, day):
		return min(getdate(day), self.today)

	def _build(self, doctype, values):
		values = {
			"doctype": doctype,
			"currency": self.currency,
			"company_currency": self.currency,
			"conversion_rate": 1,
			**values,
			"remarks": self.marker,
		}
		return frappe.get_doc(values)

	def _post(self, doc, day, field="posting_date"):
		"""Set the posting date, then insert and submit the document."""
		setattr(doc, field, day)
		if hasattr(doc, "set_posting_time"):
			doc.set_posting_time = 1
		doc.remarks = self.marker
		self._submit(doc)

	def _submit(self, doc):
		doc.flags.ignore_permissions = True
		doc.insert(ignore_permissions=True)
		doc.submit()
		frappe.get_doc(
			{
				"doctype": "Comment",
				"comment_type": "Info",
				"reference_doctype": doc.doctype,
				"reference_name": doc.name,
				"content": self.marker,
			}
		).insert(ignore_permissions=True)
		self.counts[doc.doctype] = self.counts.get(doc.doctype, 0) + 1
		return doc


def _parse_modules(modules):
	if not modules or modules == "all":
		return list(MODULES)
	chosen = [m.strip() for m in modules.split(",") if m.strip()]
	unknown = [m for m in chosen if m not in MODULES]
	if unknown:
		frappe.throw(f"Unknown modules: {', '.join(unknown)}. Choose from: {', '.join(MODULES)}")
	return chosen


def _guard(force):
	if force:
		return
	if not frappe.conf.get("developer_mode"):
		frappe.throw(
			"Refusing to seed a site without developer_mode. Enable it on a test site, "
			"or pass force=True if you know what you are doing."
		)


def _ensure_base(company=None, country="United States", currency="USD"):
	"""Make sure a company and ERPNext's base data exist, creating whatever is missing.

	Reuses the given company, else the default company, else the first company. Creates a
	demo company (with a standard chart of accounts) only when the site has none.
	"""
	# Console and CLI sessions have no language set, which breaks amount-in-words on documents.
	if not getattr(frappe.local, "lang", None):
		frappe.local.lang = frappe.db.get_single_value("System Settings", "language") or "en"

	if company:
		if not frappe.db.exists("Company", company):
			frappe.throw(f"Company {company} does not exist")
	else:
		company = frappe.db.get_single_value("Global Defaults", "default_company") or frappe.db.get_value(
			"Company", {}, "name", order_by="creation asc"
		)

	# Base data (customer groups, territories, warehouse types, price lists) comes from ERPNext's
	# setup fixtures, for the existing company's country. A company cannot be created before them.
	if not frappe.db.exists("Customer Group", {"is_group": 0}):
		from erpnext.setup.setup_wizard.operations.install_fixtures import install

		if company:
			country = frappe.db.get_value("Company", company, "country") or country
		print("Installing ERPNext base data (this takes a minute on a new site)...", flush=True)
		install(country)

	if not company:
		company = _create_demo_company(country, currency)

	if not frappe.db.get_single_value("Global Defaults", "default_company"):
		frappe.db.set_single_value("Global Defaults", "default_company", company)
	# The Desk needs a default company per user, or company-scoped pages show 'Company is mandatory'.
	for user in {"Administrator", frappe.session.user}:
		if not frappe.defaults.get_user_default("company", user=user):
			frappe.defaults.set_user_default("company", company, user=user)
	_fill_company_defaults(company)
	frappe.db.commit()
	return company


# Company default accounts that postings need, mapped to the account type that fills them.
COMPANY_DEFAULT_ACCOUNT_TYPES = {
	"default_receivable_account": "Receivable",
	"default_payable_account": "Payable",
	"default_cash_account": "Cash",
	"default_inventory_account": "Stock",
	"stock_adjustment_account": "Stock Adjustment",
	"stock_received_but_not_billed": "Stock Received But Not Billed",
	"round_off_account": "Round Off",
}


def _ensure_cost_center(company):
	"""Return the company's default cost center, creating a root and a leaf if the setup skipped them."""
	cost_center = frappe.db.get_value("Company", company, "cost_center")
	if cost_center:
		return cost_center
	root = frappe.db.get_value("Cost Center", {"company": company, "is_group": 1}, "name")
	if not root:
		root_doc = frappe.get_doc(
			{"doctype": "Cost Center", "cost_center_name": company, "company": company, "is_group": 1}
		)
		root_doc.flags.ignore_mandatory = True  # the root has no parent, as in ERPNext's company setup
		root = root_doc.insert(ignore_permissions=True).name
	leaf = frappe.get_doc(
		{"doctype": "Cost Center", "cost_center_name": "Main", "company": company, "parent_cost_center": root}
	).insert(ignore_permissions=True).name
	frappe.db.set_value("Company", company, "cost_center", leaf)
	return leaf


def _fill_company_defaults(company):
	"""Set empty default accounts and cost centers on the company, as ERPNext's setup normally does."""
	cost_center = _ensure_cost_center(company)
	doc = frappe.get_doc("Company", company)
	changed = False
	if not doc.round_off_cost_center:
		doc.round_off_cost_center = cost_center
		changed = True
	for field, account_type in COMPANY_DEFAULT_ACCOUNT_TYPES.items():
		if doc.get(field):
			continue
		account = frappe.db.get_value(
			"Account", {"company": company, "account_type": account_type, "is_group": 0}, "name"
		)
		if account:
			doc.set(field, account)
			changed = True
	if not doc.default_expense_account:
		doc.default_expense_account = frappe.db.get_value(
			"Account", {"company": company, "root_type": "Expense", "is_group": 0, "account_type": ""}, "name"
		)
		changed = True
	if not doc.default_income_account:
		doc.default_income_account = frappe.db.get_value(
			"Account", {"company": company, "root_type": "Income", "is_group": 0, "account_type": ""}, "name"
		)
		changed = True
	if changed:
		doc.flags.ignore_mandatory = True
		doc.save(ignore_permissions=True)


def _create_demo_company(country, currency):
	abbr = "SDC"
	name = "Seeder Demo Co"
	frappe.get_doc(
		{
			"doctype": "Company",
			"company_name": name,
			"abbr": abbr,
			"default_currency": currency,
			"country": country,
			"create_chart_of_accounts_based_on": "Standard Template",
			"chart_of_accounts": "Standard",
		}
	).insert(ignore_permissions=True)
	print(f"Created company {name} ({abbr}) with a standard chart of accounts")
	return name


def _first_leaf(doctype):
	name = frappe.db.get_value(doctype, {"is_group": 0}, "name")
	if not name:
		frappe.throw(f"No leaf {doctype} found. ERPNext setup should have created one.")
	return name


def _set_price(price_list, item_code, rate):
	if not frappe.db.exists("Price List", price_list):
		frappe.get_doc(
			{
				"doctype": "Price List",
				"price_list_name": price_list,
				"currency": frappe.db.get_single_value("Global Defaults", "default_currency"),
				"buying": price_list == "Standard Buying",
				"selling": price_list == "Standard Selling",
				"enabled": 1,
			}
		).insert(ignore_permissions=True)
	frappe.get_doc(
		{"doctype": "Item Price", "item_code": item_code, "price_list": price_list, "price_list_rate": rate}
	).insert(ignore_permissions=True)


def _sin_month(month):
	return math.sin(2 * math.pi * (month - 3) / 12)


def _uncovered_ranges(start, end):
	"""Parts of [start, end] that no existing fiscal year covers, as (start, end) date pairs."""
	covering = frappe.get_all("Fiscal Year", fields=["year_start_date", "year_end_date"])
	spans = sorted((row.year_start_date, row.year_end_date) for row in covering)
	ranges, cursor = [], start
	for fy_start, fy_end in spans:
		if fy_end < cursor or fy_start > end:
			continue
		if fy_start > cursor:
			ranges.append((cursor, min(add_days(fy_start, -1), end)))
		cursor = add_days(fy_end, 1)
		if cursor > end:
			return ranges
	if cursor <= end:
		ranges.append((cursor, end))
	return ranges


def _is_conflict(exc):
	text = str(exc)
	return any(marker in text for marker in ("Record has changed", "Deadlock", "Lock wait timeout", "1020", "1213"))


def _progress(percent, title):
	# Print to the terminal as well: realtime progress only reaches a browser session.
	print(f"[{percent:5.1f}%] {title}", flush=True)
	try:
		frappe.publish_progress(percent=percent, title=title)
	except Exception:
		pass
