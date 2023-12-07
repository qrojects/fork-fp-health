# -*- coding: utf-8 -*-
# Copyright (c) 2018, earthians and contributors
# For license information, please see license.txt


import json
import math

from collections import OrderedDict

import frappe
from erpnext.setup.utils import insert_record
from frappe import _
from frappe.utils import cstr, flt, get_link_to_form, rounded, time_diff_in_hours
from frappe.utils.formatters import format_value

from healthcare.healthcare.doctype.fee_validity.fee_validity import create_fee_validity
from healthcare.healthcare.doctype.healthcare_settings.healthcare_settings import (
	get_income_account,
)
from healthcare.healthcare.doctype.lab_test.lab_test import create_multiple
from healthcare.setup import setup_healthcare

from healthcare.healthcare.doctype.observation.observation import add_observation

from healthcare.healthcare.doctype.observation_template.observation_template import get_observation_template_details

from io import BytesIO
import barcode
from barcode.writer import ImageWriter
import base64

@frappe.whitelist()
def get_healthcare_services_to_invoice(patient, company):
	patient = frappe.get_doc("Patient", patient)
	items_to_invoice = []
	if patient:
		validate_customer_created(patient)
		# Customer validated, build a list of billable services
		items_to_invoice += get_appointments_to_invoice(patient, company)
		items_to_invoice += get_encounters_to_invoice(patient, company)
		items_to_invoice += get_lab_tests_to_invoice(patient, company)
		items_to_invoice += get_clinical_procedures_to_invoice(patient, company)
		items_to_invoice += get_inpatient_services_to_invoice(patient, company)
		items_to_invoice += get_therapy_plans_to_invoice(patient, company)
		items_to_invoice += get_therapy_sessions_to_invoice(patient, company)
		items_to_invoice += get_service_requests_to_invoice(patient, company)
		return items_to_invoice


def validate_customer_created(patient):
	if not frappe.db.get_value("Patient", patient.name, "customer"):
		msg = _("Please set a Customer linked to the Patient")
		msg += " <b><a href='/app/Form/Patient/{0}'>{0}</a></b>".format(patient.name)
		frappe.throw(msg, title=_("Customer Not Found"))


def get_appointments_to_invoice(patient, company):
	appointments_to_invoice = []
	patient_appointments = frappe.get_list(
		"Patient Appointment",
		fields="*",
		filters={
			"patient": patient.name,
			"company": company,
			"invoiced": 0,
			"status": ["!=", "Cancelled"],
		},
		order_by="appointment_date",
	)

	for appointment in patient_appointments:
		# Procedure Appointments
		if appointment.procedure_template:
			if frappe.db.get_value(
				"Clinical Procedure Template", appointment.procedure_template, "is_billable"
			):
				appointments_to_invoice.append(
					{
						"reference_type": "Patient Appointment",
						"reference_name": appointment.name,
						"service": appointment.procedure_template,
					}
				)
		# Consultation Appointments, should check fee validity
		else:
			if frappe.db.get_single_value(
				"Healthcare Settings", "enable_free_follow_ups"
			) and frappe.db.exists("Fee Validity Reference", {"appointment": appointment.name}):
				continue  # Skip invoicing, fee validty present
			practitioner_charge = 0
			income_account = None
			service_item = None
			if appointment.practitioner:
				details = get_service_item_and_practitioner_charge(appointment)
				service_item = details.get("service_item")
				practitioner_charge = details.get("practitioner_charge")
				income_account = get_income_account(appointment.practitioner, appointment.company)
			appointments_to_invoice.append(
				{
					"reference_type": "Patient Appointment",
					"reference_name": appointment.name,
					"service": service_item,
					"rate": practitioner_charge,
					"income_account": income_account,
				}
			)

	return appointments_to_invoice


def get_encounters_to_invoice(patient, company):
	if not isinstance(patient, str):
		patient = patient.name
	encounters_to_invoice = []
	encounters = frappe.get_list(
		"Patient Encounter",
		fields=["*"],
		filters={"patient": patient, "company": company, "invoiced": False, "docstatus": 1},
	)
	if encounters:
		for encounter in encounters:
			if not encounter.appointment:
				practitioner_charge = 0
				income_account = None
				service_item = None
				if encounter.practitioner:
					if encounter.inpatient_record and frappe.db.get_single_value(
						"Healthcare Settings", "do_not_bill_inpatient_encounters"
					):
						continue

					details = get_service_item_and_practitioner_charge(encounter)
					service_item = details.get("service_item")
					practitioner_charge = details.get("practitioner_charge")
					income_account = get_income_account(encounter.practitioner, encounter.company)

				encounters_to_invoice.append(
					{
						"reference_type": "Patient Encounter",
						"reference_name": encounter.name,
						"service": service_item,
						"rate": practitioner_charge,
						"income_account": income_account,
					}
				)

	return encounters_to_invoice


def get_lab_tests_to_invoice(patient, company):
	lab_tests_to_invoice = []
	lab_tests = frappe.get_list(
		"Lab Test",
		fields=["name", "template"],
		filters={
			"patient": patient.name,
			"company": company,
			"invoiced": False,
			"docstatus": 1,
			"service_request": "",
		},
	)
	for lab_test in lab_tests:
		item, is_billable = frappe.get_cached_value(
			"Lab Test Template", lab_test.template, ["item", "is_billable"]
		)
		if is_billable:
			lab_tests_to_invoice.append(
				{"reference_type": "Lab Test", "reference_name": lab_test.name, "service": item}
			)

	return lab_tests_to_invoice


def get_clinical_procedures_to_invoice(patient, company):
	clinical_procedures_to_invoice = []
	procedures = frappe.get_list(
		"Clinical Procedure",
		fields="*",
		filters={
			"patient": patient.name,
			"company": company,
			"invoiced": False,
			"docstatus": 1,
			"service_request": "",
		},
	)
	for procedure in procedures:
		if not procedure.appointment:
			item, is_billable = frappe.get_cached_value(
				"Clinical Procedure Template", procedure.procedure_template, ["item", "is_billable"]
			)
			if procedure.procedure_template and is_billable:
				clinical_procedures_to_invoice.append(
					{"reference_type": "Clinical Procedure", "reference_name": procedure.name, "service": item}
				)

		# consumables
		if (
			procedure.invoice_separately_as_consumables
			and procedure.consume_stock
			and procedure.status == "Completed"
			and not procedure.consumption_invoiced
		):

			service_item = frappe.db.get_single_value(
				"Healthcare Settings", "clinical_procedure_consumable_item"
			)
			if not service_item:
				msg = _("Please Configure Clinical Procedure Consumable Item in {0}").format(
					get_link_to_form("Healthcare Settings", "Healthcare Settings")
				)

				frappe.throw(msg, title=_("Missing Configuration"))

			clinical_procedures_to_invoice.append(
				{
					"reference_type": "Clinical Procedure",
					"reference_name": procedure.name,
					"service": service_item,
					"rate": procedure.consumable_total_amount,
					"description": procedure.consumption_details,
				}
			)

	return clinical_procedures_to_invoice


def get_inpatient_services_to_invoice(patient, company):
	services_to_invoice = []
	if not frappe.db.get_single_value("Healthcare Settings", "automatically_generate_billable"):
		inpatient_services = frappe.db.sql(
			"""
				SELECT
					io.*
				FROM
					`tabInpatient Record` ip, `tabInpatient Occupancy` io
				WHERE
					ip.patient=%s
					and ip.company=%s
					and io.parent=ip.name
					and io.left=1
					and io.invoiced=0
			""",
			(patient.name, company),
			as_dict=1,
		)
		for inpatient_occupancy in inpatient_services:
			service_unit_type = frappe.db.get_value(
				"Healthcare Service Unit", inpatient_occupancy.service_unit, "service_unit_type"
			)
			service_unit_type = frappe.get_cached_doc("Healthcare Service Unit Type", service_unit_type)
			if service_unit_type and service_unit_type.is_billable:
				hours_occupied = flt(
					time_diff_in_hours(inpatient_occupancy.check_out, inpatient_occupancy.check_in), 2
				)
				qty = 0.5
				if hours_occupied > 0:
					actual_qty = hours_occupied / service_unit_type.no_of_hours
					floor = math.floor(actual_qty)
					decimal_part = actual_qty - floor
					if decimal_part > 0.5:
						qty = rounded(floor + 1, 1)
					elif decimal_part < 0.5 and decimal_part > 0:
						qty = rounded(floor + 0.5, 1)
					if qty <= 0:
						qty = 0.5
				services_to_invoice.append(
					{
						"reference_type": "Inpatient Occupancy",
						"reference_name": inpatient_occupancy.name,
						"service": service_unit_type.item,
						"qty": qty,
					}
				)
			inpatient_record_doc = frappe.get_doc("Inpatient Record", inpatient_occupancy.parent)
			for item in inpatient_record_doc.items:
				if item.stock_entry and not item.invoiced:
					services_to_invoice.append(
					{
						"reference_type": "Inpatient Record Item",
						"reference_name": item.name,
						"service": item.item_code,
						"qty": item.quantity,
					}
				)

	else:
		inpatient_services = frappe.db.sql(
			"""
				SELECT
					iri.*
				FROM
					`tabInpatient Record` ip, `tabInpatient Record Item` iri
				WHERE
					ip.patient=%s
					and ip.company=%s
					and iri.parent=ip.name
					and iri.invoiced=0
			""",
			(patient.name, company),
			as_dict=1,
		)

		for inpatient_occupancy in inpatient_services:
			services_to_invoice.append(
				{
					"reference_type": "Inpatient Record Item",
					"reference_name": inpatient_occupancy.name,
					"service": inpatient_occupancy.item_code,
					"qty": inpatient_occupancy.quantity,
				}
			)
	return services_to_invoice


def get_therapy_plans_to_invoice(patient, company):
	therapy_plans_to_invoice = []
	therapy_plans = frappe.get_list(
		"Therapy Plan",
		fields=["therapy_plan_template", "name"],
		filters={
			"patient": patient.name,
			"invoiced": 0,
			"company": company,
			"therapy_plan_template": ("!=", ""),
			"docstatus": 1,
		},
	)
	for plan in therapy_plans:
		therapy_plans_to_invoice.append(
			{
				"reference_type": "Therapy Plan",
				"reference_name": plan.name,
				"service": frappe.db.get_value(
					"Therapy Plan Template", plan.therapy_plan_template, "linked_item"
				),
			}
		)

	return therapy_plans_to_invoice


def get_therapy_sessions_to_invoice(patient, company):
	therapy_sessions_to_invoice = []
	therapy_plans = frappe.db.get_all("Therapy Plan", {"therapy_plan_template": ("!=", "")})
	therapy_plans_created_from_template = []
	for entry in therapy_plans:
		therapy_plans_created_from_template.append(entry.name)

	therapy_sessions = frappe.get_list(
		"Therapy Session",
		fields="*",
		filters={
			"patient": patient.name,
			"invoiced": 0,
			"company": company,
			"therapy_plan": ("not in", therapy_plans_created_from_template),
			"docstatus": 1,
			"service_request": "",
		},
	)
	for therapy in therapy_sessions:
		if not therapy.appointment:
			if therapy.therapy_type and frappe.db.get_value(
				"Therapy Type", therapy.therapy_type, "is_billable"
			):
				therapy_sessions_to_invoice.append(
					{
						"reference_type": "Therapy Session",
						"reference_name": therapy.name,
						"service": frappe.db.get_value("Therapy Type", therapy.therapy_type, "item"),
					}
				)

	return therapy_sessions_to_invoice


def get_service_requests_to_invoice(patient, company):
	orders_to_invoice = []
	service_requests = frappe.get_list(
		"Service Request",
		fields=["*"],
		filters={
			"patient": patient.name,
			"company": company,
			"billing_status": "Pending",
			"docstatus": 1,
			"template_dt": ["not in", ["Healthcare Activity", "Appointment Type"]],
		},
	)

	for service_request in service_requests:
		item, is_billable = frappe.get_cached_value(
			service_request.template_dt, service_request.template_dn, ["item", "is_billable"]
		)
		price_list, price_list_currency = frappe.db.get_values(
			"Price List", {"selling": 1}, ["name", "currency"]
		)[0]
		args = {
			"doctype": "Sales Invoice",
			"item_code": item,
			"company": service_request.get("company"),
			"customer": frappe.db.get_value("Patient", service_request.get("patient"), "customer"),
			"plc_conversion_rate": 1.0,
			"conversion_rate": 1.0,
		}
		if is_billable:
			orders_to_invoice.append(
				{
					"reference_type": "Service Request",
					"reference_name": service_request.name,
					"service": item,
					"qty": service_request.quantity if service_request.quantity else 1,
				}
			)
	return orders_to_invoice


@frappe.whitelist()
def get_service_item_and_practitioner_charge(doc):
	if isinstance(doc, str):
		doc = json.loads(doc)
		doc = frappe.get_doc(doc)

	service_item = None
	practitioner_charge = None
	department = doc.medical_department if doc.doctype == "Patient Encounter" else doc.department

	is_inpatient = doc.inpatient_record

	if doc.get("appointment_type"):
		service_item, practitioner_charge = get_appointment_type_service_item(
			doc.appointment_type, department, is_inpatient
		)

	if not service_item and not practitioner_charge:
		service_item, practitioner_charge = get_practitioner_service_item(doc.practitioner, is_inpatient)
		if not service_item:
			service_item = get_healthcare_service_item(is_inpatient)

	if not service_item:
		throw_config_service_item(is_inpatient)

	if not practitioner_charge:
		throw_config_practitioner_charge(is_inpatient, doc.practitioner)

	return {"service_item": service_item, "practitioner_charge": practitioner_charge}


def get_appointment_type_service_item(appointment_type, department, is_inpatient):
	from healthcare.healthcare.doctype.appointment_type.appointment_type import (
		get_service_item_based_on_department,
	)

	item_list = get_service_item_based_on_department(appointment_type, department)
	service_item = None
	practitioner_charge = None

	if item_list:
		if is_inpatient:
			service_item = item_list.get("inpatient_visit_charge_item")
			practitioner_charge = item_list.get("inpatient_visit_charge")
		else:
			service_item = item_list.get("op_consulting_charge_item")
			practitioner_charge = item_list.get("op_consulting_charge")

	return service_item, practitioner_charge


def throw_config_service_item(is_inpatient):
	service_item_label = _("Out Patient Consulting Charge Item")
	if is_inpatient:
		service_item_label = _("Inpatient Visit Charge Item")

	msg = _(
		("Please Configure {0} in ").format(service_item_label)
		+ """<b><a href='/app/Form/Healthcare Settings'>Healthcare Settings</a></b>"""
	)
	frappe.throw(msg, title=_("Missing Configuration"))


def throw_config_practitioner_charge(is_inpatient, practitioner):
	charge_name = _("OP Consulting Charge")
	if is_inpatient:
		charge_name = _("Inpatient Visit Charge")

	msg = _(
		("Please Configure {0} for Healthcare Practitioner").format(charge_name)
		+ """ <b><a href='/app/Form/Healthcare Practitioner/{0}'>{0}</a></b>""".format(practitioner)
	)
	frappe.throw(msg, title=_("Missing Configuration"))


def get_practitioner_service_item(practitioner, is_inpatient):
	service_item = None
	practitioner_charge = None

	if is_inpatient:
		service_item, practitioner_charge = frappe.db.get_value(
			"Healthcare Practitioner",
			practitioner,
			["inpatient_visit_charge_item", "inpatient_visit_charge"],
		)
	else:
		service_item, practitioner_charge = frappe.db.get_value(
			"Healthcare Practitioner", practitioner, ["op_consulting_charge_item", "op_consulting_charge"]
		)

	return service_item, practitioner_charge


def get_healthcare_service_item(is_inpatient):
	service_item = None

	if is_inpatient:
		service_item = frappe.db.get_single_value("Healthcare Settings", "inpatient_visit_charge_item")
	else:
		service_item = frappe.db.get_single_value("Healthcare Settings", "op_consulting_charge_item")

	return service_item


def get_practitioner_charge(practitioner, is_inpatient):
	if is_inpatient:
		practitioner_charge = frappe.db.get_value(
			"Healthcare Practitioner", practitioner, "inpatient_visit_charge"
		)
	else:
		practitioner_charge = frappe.db.get_value(
			"Healthcare Practitioner", practitioner, "op_consulting_charge"
		)
	if practitioner_charge:
		return practitioner_charge
	return False


def manage_invoice_validate(doc, method):
	if doc.service_unit and len(doc.items):
		for item in doc.items:
			if not item.service_unit:
				item.service_unit = doc.service_unit


def manage_invoice_submit_cancel(doc, method):
	if doc.items:
		for item in doc.items:
			if item.get("reference_dt") and item.get("reference_dn"):
				# TODO check
				# if frappe.get_meta(item.reference_dt).has_field("invoiced"):
				set_invoiced(item, method, doc.name)
		if method == "on_submit":
			create_sample_collection_and_observation(doc)

	if method == "on_submit" and frappe.db.get_single_value(
		"Healthcare Settings", "create_lab_test_on_si_submit"
	):
		create_multiple("Sales Invoice", doc.name)


def set_invoiced(item, method, ref_invoice=None):
	invoiced = False
	if method == "on_submit":
		validate_invoiced_on_submit(item)
		invoiced = True

	if item.reference_dt == "Clinical Procedure":
		service_item = frappe.db.get_single_value(
			"Healthcare Settings", "clinical_procedure_consumable_item"
		)
		if service_item == item.item_code:
			frappe.db.set_value(item.reference_dt, item.reference_dn, "consumption_invoiced", invoiced)
		else:
			frappe.db.set_value(item.reference_dt, item.reference_dn, "invoiced", invoiced)
	else:
		if item.reference_dt not in ["Service Request", "Medication Request"]:
			frappe.db.set_value(item.reference_dt, item.reference_dn, "invoiced", invoiced)

	if item.reference_dt == "Patient Appointment":
		if frappe.db.get_value("Patient Appointment", item.reference_dn, "procedure_template"):
			dt_from_appointment = "Clinical Procedure"
		else:
			dt_from_appointment = "Patient Encounter"
		manage_doc_for_appointment(dt_from_appointment, item.reference_dn, invoiced)

	elif item.reference_dt == "Lab Prescription":
		manage_prescriptions(
			invoiced, item.reference_dt, item.reference_dn, "Lab Test", "lab_test_created"
		)

	elif item.reference_dt == "Procedure Prescription":
		manage_prescriptions(
			invoiced, item.reference_dt, item.reference_dn, "Clinical Procedure", "procedure_created"
		)
	elif item.reference_dt in ["Service Request", "Medication Request"]:
		# if order is invoiced, set both order and service transaction as invoiced
		hso = frappe.get_doc(item.reference_dt, item.reference_dn)
		if invoiced:
			hso.update_invoice_details(item.qty)
		else:
			hso.update_invoice_details(item.qty * -1)

		# service transaction linking to HSO
		if item.reference_dt == "Service Request":
			template_map = {
				"Clinical Procedure Template": "Clinical Procedure",
				"Therapy Type": "Therapy Session",
				"Lab Test Template": "Lab Test"
				# 'Healthcare Service Unit': 'Inpatient Occupancy'
			}



def validate_invoiced_on_submit(item):
	if (
		item.reference_dt == "Clinical Procedure"
		and frappe.db.get_single_value("Healthcare Settings", "clinical_procedure_consumable_item")
		== item.item_code
	):
		is_invoiced = frappe.db.get_value(item.reference_dt, item.reference_dn, "consumption_invoiced")

	elif item.reference_dt in ["Service Request", "Medication Request"]:
		billing_status = frappe.db.get_value(item.reference_dt, item.reference_dn, "billing_status")
		is_invoiced = True if billing_status == "Invoiced" else False

	else:
		is_invoiced = frappe.db.get_value(item.reference_dt, item.reference_dn, "invoiced")
	if is_invoiced:
		frappe.throw(
			_("The item referenced by {0} - {1} is already invoiced").format(
				item.reference_dt, item.reference_dn
			)
		)


def manage_prescriptions(invoiced, ref_dt, ref_dn, dt, created_check_field):
	created = frappe.db.get_value(ref_dt, ref_dn, created_check_field)
	if created:
		# Fetch the doc created for the prescription
		doc_created = frappe.db.get_value(dt, {"prescription": ref_dn})
		frappe.db.set_value(dt, doc_created, "invoiced", invoiced)


def check_fee_validity(appointment):
	if not frappe.db.get_single_value("Healthcare Settings", "enable_free_follow_ups"):
		return

	validity = frappe.db.exists(
		"Fee Validity",
		{
			"practitioner": appointment.practitioner,
			"patient": appointment.patient,
			"valid_till": (">=", appointment.appointment_date),
		},
	)
	if not validity:
		return

	validity = frappe.get_doc("Fee Validity", validity)
	return validity


def manage_fee_validity(appointment):
	fee_validity = check_fee_validity(appointment)

	if fee_validity:
		if appointment.status == "Cancelled" and fee_validity.visited > 0:
			fee_validity.visited -= 1
			frappe.db.delete("Fee Validity Reference", {"appointment": appointment.name})
		elif fee_validity.status == "Completed":
			return
		else:
			fee_validity.visited += 1
			fee_validity.append("ref_appointments", {"appointment": appointment.name})
		fee_validity.save(ignore_permissions=True)
	else:
		fee_validity = create_fee_validity(appointment)
	return fee_validity


def manage_doc_for_appointment(dt_from_appointment, appointment, invoiced):
	dn_from_appointment = frappe.db.get_value(
		dt_from_appointment, filters={"appointment": appointment}
	)
	if dn_from_appointment:
		frappe.db.set_value(dt_from_appointment, dn_from_appointment, "invoiced", invoiced)


@frappe.whitelist()
def get_drugs_to_invoice(encounter=None, patient=None):
	customer = None
	if encounter:
		patient = frappe.db.get_value("Patient Encounter", encounter, "patient")
		if patient:
			customer = frappe.db.get_value("Patient", patient, "customer")
	elif patient:
		customer = frappe.db.get_value("Patient", patient, "customer")
	if customer:
		orders_to_invoice = []
		filters = {
			"patient": patient,
			"billing_status": ["in", ["Pending", "Partly Invoiced"]],
			"docstatus": 1,
		}
		if encounter:
			filters["order_group"] = encounter
		medication_requests = frappe.get_list(
			"Medication Request",
			fields=["*"],
			filters=filters,
		)
		for medication_request in medication_requests:
			is_billable = frappe.get_cached_value(
				"Medication", medication_request.medication, ["is_billable"]
			)

			description = ""
			if medication_request.dosage and medication_request.period:
				description = _("{0} for {1}").format(medication_request.dosage, medication_request.period)

			if medication_request.medication_item and is_billable and is_billable[0]==1:
				billable_order_qty = medication_request.get("quantity", 1) - medication_request.get(
					"qty_invoiced", 0
				)
				if medication_request.number_of_repeats_allowed:
					if (
						medication_request.total_dispensable_quantity
						>= medication_request.quantity + medication_request.qty_invoiced
					):
						billable_order_qty = medication_request.get("quantity", 1)
					else:
						billable_order_qty = (
							medication_request.total_dispensable_quantity - medication_request.get("qty_invoiced", 0)
						)

				orders_to_invoice.append(
					{
						"reference_type": "Medication Request",
						"reference_name": medication_request.name,
						"drug_code": medication_request.medication_item,
						"quantity": billable_order_qty,
						"description": description,
					}
				)
		return orders_to_invoice



@frappe.whitelist()
def get_children(doctype, parent=None, company=None, is_root=False):
	parent_fieldname = "parent_" + doctype.lower().replace(" ", "_")
	fields = ["name as value", "is_group as expandable", "lft", "rgt"]

	filters = [["ifnull(`{0}`,'')".format(parent_fieldname), "=", "" if is_root else parent]]

	if is_root:
		fields += ["service_unit_type"] if doctype == "Healthcare Service Unit" else []
		filters.append(["company", "=", company])
	else:
		fields += (
			["service_unit_type", "allow_appointments", "inpatient_occupancy", "occupancy_status"]
			if doctype == "Healthcare Service Unit"
			else []
		)
		fields += [parent_fieldname + " as parent"]

	service_units = frappe.get_list(doctype, fields=fields, filters=filters)
	for each in service_units:
		if each["expandable"] == 1:  # group node
			available_count = frappe.db.count(
				"Healthcare Service Unit",
				filters={"parent_healthcare_service_unit": each["value"], "inpatient_occupancy": 1},
			)

			if available_count > 0:
				occupied_count = frappe.db.count(
					"Healthcare Service Unit",
					{
						"parent_healthcare_service_unit": each["value"],
						"inpatient_occupancy": 1,
						"occupancy_status": "Occupied",
					},
				)
				# set occupancy status of group node
				each["occupied_of_available"] = str(occupied_count) + " Occupied of " + str(available_count)

	return service_units


@frappe.whitelist()
def get_patient_vitals(patient, from_date=None, to_date=None):
	if not patient:
		return

	vitals = frappe.db.get_all(
		"Vital Signs",
		filters={"docstatus": 1, "patient": patient},
		order_by="signs_date, signs_time",
		fields=["*"],
	)

	if len(vitals):
		return vitals
	return False


@frappe.whitelist()
def render_docs_as_html(docs):
	# docs key value pair {doctype: docname}
	docs_html = "<div class='col-md-12 col-sm-12 text-muted'>"
	for doc in docs:
		docs_html += render_doc_as_html(doc["doctype"], doc["docname"])["html"] + "<br/>"
		return {"html": docs_html}


@frappe.whitelist()
def render_doc_as_html(doctype, docname, exclude_fields=None):
	"""
	Render document as HTML
	"""
	exclude_fields = exclude_fields or []
	doc = frappe.get_doc(doctype, docname)
	meta = frappe.get_meta(doctype)
	doc_html = section_html = section_label = html = ""
	sec_on = has_data = False
	col_on = 0

	for df in meta.fields:
		# on section break append previous section and html to doc html
		if df.fieldtype == "Section Break":
			if has_data and col_on and sec_on:
				doc_html += section_html + html + "</div>"

			elif has_data and not col_on and sec_on:
				doc_html += """
					<br>
					<div class='row'>
						<div class='col-md-12 col-sm-12'>
							<b>{0}</b>
						</div>
					</div>
					<div class='row'>
						<div class='col-md-12 col-sm-12'>
							{1} {2}
						</div>
					</div>
				""".format(
					section_label, section_html, html
				)

			# close divs for columns
			while col_on:
				doc_html += "</div>"
				col_on -= 1

			sec_on = True
			has_data = False
			col_on = 0
			section_html = html = ""

			if df.label:
				section_label = df.label
			continue

		# on column break append html to section html or doc html
		if df.fieldtype == "Column Break":
			if sec_on and not col_on and has_data:
				section_html += """
					<br>
					<div class='row'>
						<div class='col-md-12 col-sm-12'>
							<b>{0}</b>
						</div>
					</div>
					<div class='row'>
						<div class='col-md-4 col-sm-4'>
							{1}
						</div>
				""".format(
					section_label, html
				)
			elif col_on == 1 and has_data:
				section_html += "<div class='col-md-4 col-sm-4'>" + html + "</div>"
			elif col_on > 1 and has_data:
				doc_html += "<div class='col-md-4 col-sm-4'>" + html + "</div>"
			else:
				doc_html += """
					<div class='row'>
						<div class='col-md-12 col-sm-12'>
							{0}
						</div>
					</div>
				""".format(
					html
				)

			html = ""
			col_on += 1

			if df.label:
				html += "<br>" + df.label
			continue

		# on table iterate through items and create table
		# based on the in_list_view property
		# append to section html or doc html
		if df.fieldtype == "Table":
			items = doc.get(df.fieldname)
			if not items:
				continue
			child_meta = frappe.get_meta(df.options)

			if not has_data:
				has_data = True
			table_head = table_row = ""
			create_head = True

			for item in items:
				table_row += "<tr>"
				for cdf in child_meta.fields:
					if cdf.in_list_view:
						if create_head:
							table_head += "<th class='text-muted'>" + cdf.label + "</th>"
						if item.get(cdf.fieldname):
							table_row += "<td>" + cstr(item.get(cdf.fieldname)) + "</td>"
						else:
							table_row += "<td></td>"

				create_head = False
				table_row += "</tr>"

			if sec_on:
				section_html += """
					<table class='table table-condensed bordered'>
						{0} {1}
					</table>
				""".format(
					table_head, table_row
				)
			else:
				html += """
					<table class='table table-condensed table-bordered'>
						{0} {1}
					</table>
				""".format(
					table_head, table_row
				)
			continue

		# on any other field type add label and value to html
		if (
			not df.hidden
			and not df.print_hide
			and doc.get(df.fieldname)
			and df.fieldname not in exclude_fields
		):
			formatted_value = format_value(doc.get(df.fieldname), meta.get_field(df.fieldname), doc)
			html += "<br>{0} : {1}".format(df.label or df.fieldname, formatted_value)

			if not has_data:
				has_data = True

	if sec_on and col_on and has_data:
		doc_html += section_html + html + "</div></div>"
	elif sec_on and not col_on and has_data:
		doc_html += """
			<div class='col-md-12 col-sm-12'>
				<div class='col-md-12 col-sm-12'>
					{0} {1}
				</div>
			</div>
		""".format(
			section_html, html
		)
	return {"html": doc_html}


def update_address_links(address, method):
	"""
	Hook validate Address
	If Patient is linked in Address, also link the associated Customer
	"""
	if "Healthcare" not in frappe.get_active_domains():
		return

	patient_links = list(filter(lambda link: link.get("link_doctype") == "Patient", address.links))

	for link in patient_links:
		customer = frappe.db.get_value("Patient", link.get("link_name"), "customer")
		if customer and not address.has_link("Customer", customer):
			address.append("links", dict(link_doctype="Customer", link_name=customer))


def update_patient_email_and_phone_numbers(contact, method):
	"""
	Hook validate Contact
	Update linked Patients' primary mobile and phone numbers
	"""
	if "Healthcare" not in frappe.get_active_domains() or contact.flags.skip_patient_update:
		return

	if contact.is_primary_contact and (contact.email_id or contact.mobile_no or contact.phone):
		patient_links = list(filter(lambda link: link.get("link_doctype") == "Patient", contact.links))

		for link in patient_links:
			contact_details = frappe.db.get_value(
				"Patient", link.get("link_name"), ["email", "mobile", "phone"], as_dict=1
			)
			if contact.email_id and contact.email_id != contact_details.get("email"):
				frappe.db.set_value("Patient", link.get("link_name"), "email", contact.email_id)
			if contact.mobile_no and contact.mobile_no != contact_details.get("mobile"):
				frappe.db.set_value("Patient", link.get("link_name"), "mobile", contact.mobile_no)
			if contact.phone and contact.phone != contact_details.get("phone"):
				frappe.db.set_value("Patient", link.get("link_name"), "phone", contact.phone)


def before_tests():
	# complete setup if missing
	from frappe.desk.page.setup_wizard.setup_wizard import setup_complete

	if not frappe.get_list("Company"):
		setup_complete(
			{
				"currency": "INR",
				"full_name": "Test User",
				"company_name": "Frappe Care LLC",
				"timezone": "America/New_York",
				"company_abbr": "WP",
				"industry": "Healthcare",
				"country": "United States",
				"fy_start_date": "2022-04-01",
				"fy_end_date": "2023-03-31",
				"language": "english",
				"company_tagline": "Testing",
				"email": "test@erpnext.com",
				"password": "test",
				"chart_of_accounts": "Standard",
				"domains": ["Healthcare"],
			}
		)

		setup_healthcare()


def create_healthcare_service_unit_tree_root(doc, method=None):
	record = [
		{
			"doctype": "Healthcare Service Unit",
			"healthcare_service_unit_name": "All Healthcare Service Units",
			"is_group": 1,
			"company": doc.name,
		}
	]
	insert_record(record)


def validate_nursing_tasks(document):
	if not frappe.db.get_single_value("Healthcare Settings", "validate_nursing_checklists"):
		return True

	filters = {
		"reference_name": document.name,
		"mandatory": 1,
		"status": ["not in", ["Completed", "Cancelled"]],
	}
	tasks = frappe.get_all("Nursing Task", filters=filters)
	if not tasks:
		return True

	frappe.throw(
		_("Please complete linked Nursing Tasks before submission: {}").format(
			", ".join(get_link_to_form("Nursing Task", task.name) for task in tasks)
		)
	)


@frappe.whitelist()
def get_medical_codes(template_dt, template_dn, code_standard=None):
	"""returns codification table from templates"""
	filters = {"parent": template_dn, "parenttype": template_dt}

	if code_standard:
		filters["medical_code_standard"] = code_standard

	return frappe.db.get_all(
		"Codification Table",
		filters=filters,
		fields=[
			"medical_code",
			"code",
			"system",
			"description",
			"medical_code_standard",
		],
	)


def company_on_trash(doc, method):
	for su in frappe.get_all("Healthcare Service Unit", {"company": doc.name}):
		service_unit_doc = frappe.get_doc("Healthcare Service Unit", su.get("name"))
		service_unit_doc.flags.on_trash_company = True
		service_unit_doc.delete()


def create_sample_collection_and_observation(doc):
	meta = frappe.get_meta("Sales Invoice Item", cached=True)
	diag_report_required = False
	data = []
	for item in doc.items:
		# to set patient in item table if not set
		if meta.has_field("patient") and not item.patient:
			item.patient = doc.patient

		# ignore if already created from service request
		if item.get("reference_dt") == "Service Request" and item.get("reference_dn"):
			if frappe.db.exists(
				"Observation Sample Collection", {"service_request": item.get("reference_dn")}
			) or frappe.db.exists(
				"Sample Collection", {"service_request": item.get("reference_dn")}
			):
				continue

		template_id = frappe.db.exists(
			"Observation Template", {"item": item.item_code}
		)
		if template_id:
			temp_dict = {}
			temp_dict["name"] = template_id
			if meta.has_field("patient") and item.get("patient"):
				temp_dict["patient"] = item.get("patient")
				temp_dict["child"] = item.get("name")
			data.append(temp_dict)

	out_data = []
	for d in data:
		observation_template = frappe.get_value(
				"Observation Template",
				d.get("name"),
				[
					"sample_type",
					"sample",
					"medical_department",
					"container_closure_color",
					"name",
					"sample_qty",
					"has_component",
					"sample_collection_required",
				],
				as_dict=True,
			)
		if observation_template:
			observation_template["patient"] = d.get("patient")
			observation_template["child"] = d.get("child")
			out_data.append(observation_template)

	if not meta.has_field("patient"):
		sample_collection = create_sample_collection(doc, doc.patient)
	else:
		grouped = {}
		for grp in out_data:
			grouped.setdefault(grp.patient, []).append(grp)
		if grouped:
			out_data = grouped

	for grp in out_data:
		patient  = doc.patient
		if meta.has_field("patient") and grp:
			patient = grp

		if meta.has_field("patient"):
			sample_collection = create_sample_collection(doc, patient)
			for obs in out_data[grp]:
				(
					sample_collection,
					diag_report_required,
				) = insert_observation_and_sample_collection(
					doc, patient, obs, sample_collection, obs.get("child")
				)
			if (
				sample_collection
				and len(sample_collection.get("observation_sample_collection")) > 0
			):
				sample_collection.save(ignore_permissions=True)

			if diag_report_required:
				insert_diagnostic_report(doc, patient, sample_collection.name)
		else:
			sample_collection, diag_report_required = insert_observation_and_sample_collection(
				doc, patient, grp, sample_collection
			)


	if not meta.has_field("patient"):
		if (
			sample_collection
			and len(sample_collection.get("observation_sample_collection")) > 0
		):
			sample_collection.save(ignore_permissions=True)

		if diag_report_required:
			insert_diagnostic_report(doc, patient, sample_collection.name)


def create_sample_collection(doc, patient):
	patient = frappe.get_doc("Patient", patient)
	sample_collection = frappe.new_doc("Sample Collection")
	sample_collection.patient = patient.name
	sample_collection.patient_age = patient.get_age()
	sample_collection.patient_sex = patient.sex
	sample_collection.company = doc.company
	sample_collection.referring_practitioner = doc.ref_practitioner
	sample_collection.reference_doc = doc.doctype
	sample_collection.reference_name = doc.name
	return sample_collection

def insert_diagnostic_report(doc, patient, sample_collection=None):
	diagnostic_report = frappe.new_doc("Diagnostic Report")
	diagnostic_report.company = doc.company
	diagnostic_report.patient = patient
	diagnostic_report.ref_doctype = doc.doctype
	diagnostic_report.docname = doc.name
	diagnostic_report.practitioner = doc.ref_practitioner
	diagnostic_report.sample_collection = sample_collection
	diagnostic_report.save(ignore_permissions=True)

def insert_observation_and_sample_collection(doc, patient, grp, sample_collection, child = None):
	diag_report_required = False
	if grp.get("has_component"):
		diag_report_required = True
		# parent observation
		parent_observation = add_observation(
				patient,
				grp.get("name"),
				practitioner=doc.ref_practitioner,
				invoice=doc.name,
				child = child if child else "",
			)

		sample_reqd_component_obs, non_sample_reqd_component_obs = get_observation_template_details(grp.get("name"))
		# create observation for non sample_collection_reqd grouped templates

		if len(non_sample_reqd_component_obs)>0:
			for comp in non_sample_reqd_component_obs:
				add_observation(
					patient,
					comp,
					practitioner=doc.ref_practitioner,
					parent=parent_observation,
					invoice=doc.name,
					child = child if child else "",
				)
		# create sample_colleciton child row for  sample_collection_reqd grouped templates
		if len(sample_reqd_component_obs)>0:
			sample_collection.append(
				"observation_sample_collection",
				{
					"observation_template": grp.get("name"),
					"container_closure_color": grp.get("color"),
					"sample": grp.get("sample"),
					"sample_type": grp.get("sample_type"),
					"component_observation_parent": parent_observation,
					"reference_child" : child if child else "",
				},
			)

	else:
		diag_report_required = True
		# create observation for non sample_collection_reqd individual templates
		if not grp.get("sample_collection_required"):
			add_observation(
				patient,
				grp.get("name"),
				practitioner=doc.ref_practitioner,
				invoice=doc.name,
				child = child if child else "",
			)
		else:
			# create sample_colleciton child row for  sample_collection_reqd individual templates
			sample_collection.append(
				"observation_sample_collection",
				{
					"observation_template": grp.get("name"),
					"container_closure_color": grp.get("color"),
					"sample": grp.get("sample"),
					"sample_type": grp.get("sample_type"),
					"reference_child" : child if child else "",
				},
			)
	return sample_collection, diag_report_required


@frappe.whitelist()
def generate_barcodes(in_val):
	from io import BytesIO
	from barcode import Code128
	from barcode.writer import ImageWriter

	stream = BytesIO()
	Code128(str(in_val), writer=ImageWriter()).write(
		stream,
		{
			"module_height": 3,
			"text_distance": 0.9,
			"write_text": False,
		},
	)
	barcode_base64 = base64.b64encode(stream.getbuffer()).decode()
	stream.close()

	return barcode_base64
