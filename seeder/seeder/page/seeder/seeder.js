frappe.pages["seeder"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({ parent: wrapper, title: __("Seeder"), single_column: true });

	const form = new frappe.ui.FieldGroup({
		parent: page.main,
		fields: [
			{ fieldtype: "Select", fieldname: "kind", label: __("Seed"), options: "erpnext\nhrms", default: "erpnext", reqd: 1 },
			{ fieldtype: "Int", fieldname: "years", label: __("Years of history"), default: 3 },
			{ fieldtype: "Int", fieldname: "records", label: __("Records (ERPNext)"), default: 2000 },
			{ fieldtype: "Int", fieldname: "employees", label: __("Employees (HRMS)"), default: 20 },
			{ fieldtype: "Data", fieldname: "company", label: __("Company (blank = default)") },
			{ fieldtype: "Check", fieldname: "reset", label: __("Remove previously seeded records first") },
		],
	});
	form.make();

	page.set_primary_action(__("Run"), () => {
		const values = form.get_values();
		if (!values) return;
		frappe.call({ method: "seeder.api.start_seed", args: values }).then(() => {
			frappe.show_alert({
				message: __("Seeding started. Progress will show here; the Desk will tell you when it finishes."),
				indicator: "green",
			});
		});
	});
};
