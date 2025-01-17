#!/usr/bin/env python3

import os
import sys
import json
import envdir
import gspread
import requests
import xlsxwriter
from datetime import datetime as dt
import pandas as pd
from pathlib import Path
from urllib.parse import urlparse
from oauth2client.service_account import ServiceAccountCredentials

base_dir = os.path.abspath(__file__ + "/../../")
envdir.open(os.path.join(base_dir, '.env/redcap'))
sys.path.append(base_dir)

# pylint: disable=import-error, wrong-import-position
from etc.scan_tphcd_dashboard_config import project_dict


def main():
    # config files for variable and zipcode mapping
    with open(os.path.join(base_dir, 'etc/zipcode_county_map.json'), 'r') as f:
        zipcode_county_map = json.load(f)

    print("Connecting to Google Sheets")
    # creates conneciton to google sheets
    client = get_gspread_client(
        os.path.join(base_dir,
                     '.config/logistics-db-1615935272839-a608db2dc31d'))

    # links variables to SHARED_TPCHD_SCAN_Metrics Google Sheets
    sheet = client.open('TPCHD Dashboard')

    # Export all records from SCAN redcap
    print('Getting REDCap data')
    data = pd.DataFrame(get_redcap_data())
    data = data.replace('', pd.NA)

    # Filter to pierce county by zipcode
    data = filter_pierce(data, zipcode_county_map)

    # Import to SHARED_TPCHD_SCAN_Metrics Google Sheets
    print('Importing data')
    import_prio_code(data, sheet.worksheet('Priority Code'))
    import_enrollment(data, sheet.worksheet('Enrollment'))
    import_zipcode(data, sheet.worksheet('Zipcode'))
    import_age(data, sheet.worksheet('Age'))
    import_positive(data, sheet.worksheet('Positive'))

    download_data(sheet)


def get_gspread_client(auth_file):
    scope = [
        'https://spreadsheets.google.com/feeds',
        'https://www.googleapis.com/auth/drive'
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_name(auth_file, scope)
    return (gspread.authorize(creds))


def get_redcap_data():
    export_feilds = [
        'record_id', 'redcap_event_name', 'home_zipcode_2', 'priority_code',
        'age', 'date_tested', 'test_result', 'illness_q_date'
    ]
    projects = ['SCAN English', 'SCAN Spanish', 'SCAN Vietnamese']
    url = urlparse(os.environ.get("REDCAP_API_URL"))
    data = []

    for p in projects:
        formData = {
            'token':
            os.environ.get(
                f"REDCAP_API_TOKEN_{url.netloc}_{project_dict[p]['project_id']}"
            ),
            'content':
            'record',
            'format':
            'json',
            'type':
            'flat',
            'fields':
            ",".join(map(str, export_feilds)),
            'rawOrLabel':
            'label',
            'returnFormat':
            'json',
            'filterLogic':
            '[event-name][illness_q_date] <> ""'
        }
        r = requests.post(url.geturl(), data=formData)
        data.extend(r.json())
    return (data)


def filter_pierce(data, zipcode_county_map):
    data = data.loc[data['home_zipcode_2'].isin(
        zipcode_county_map['SCAN PIERCE'])]
    '''Due to the inclusion of zipcode 98092 in Pierce county, a date cutoff is needed in order to
	remove past enrollments to this zipcode when it was defined as being part of King County'''
    data = data[((data['home_zipcode_2'] == '98092') &
                 (data['illness_q_date'] > '2021-09-16')) |
                (data['home_zipcode_2'] != '98092')]
    return (data)


def import_prio_code(data, sheet):
    print('Importing Priority Code Data')
    data = data.dropna(subset=['priority_code']).groupby(
        ['illness_q_date', 'priority_code'],
        as_index=False).agg({'record_id': 'count'})
    sheet.delete_rows(2, sheet.row_count)
    sheet.append_rows(data.values.tolist(), value_input_option='USER_ENTERED')


def import_enrollment(data, sheet):
    print('Importing Enrollment Data')
    data = data.dropna(subset=['illness_q_date']).groupby(
        ['illness_q_date'], as_index=False).agg({'record_id': 'count'})
    sheet.update('A2:B1000',
                 data.values.tolist(),
                 value_input_option='USER_ENTERED')


def import_zipcode(data, sheet):
    print('Importing Zipcode Data')
    data = data.dropna(subset=['illness_q_date']).groupby(
        ['illness_q_date', 'home_zipcode_2'],
        as_index=False).agg({'record_id': 'count'})
    sheet.delete_rows(2, sheet.row_count)
    sheet.append_rows(data.values.tolist(), value_input_option='USER_ENTERED')


def import_age(data, sheet):
    print('Importing Age Data')
    data['age bucket'] = data['age'].apply(
        lambda row: get_age_bucket(int(row)))
    data = data.dropna(subset=['illness_q_date']).groupby(
        ['illness_q_date', 'age bucket'],
        as_index=False).agg({'record_id': 'count'})
    sheet.delete_rows(2, sheet.row_count)
    sheet.append_rows(data.values.tolist(), value_input_option='USER_ENTERED')


def import_positive(data, sheet):
    print('Importing Positive Data')
    data = data.dropna(subset=['test_result']).groupby(
        ['illness_q_date', 'test_result'],
        as_index=False).agg({'record_id': 'count'})
    sheet.delete_rows(2, sheet.row_count)
    sheet.append_rows(data.values.tolist(), value_input_option='USER_ENTERED')


def get_age_bucket(age):
    if age >= 80:
        return ('80+ years')
    elif age >= 70:
        return ('70-79 years')
    elif age >= 60:
        return ('60-69 years')
    elif age >= 50:
        return ('50-59 years')
    elif age >= 40:
        return ('40-49 years')
    elif age >= 30:
        return ('30-39 years')
    elif age >= 20:
        return ('20-29 years')
    elif age >= 0:
        return ('0-19 years')
    else:
        return ('unknown')


#find next available row in a given sheet
def next_available_row(worksheet):
    str_list = list(filter(None, worksheet.col_values(1)))
    return len(str_list) + 1


#download the data in .xlsx format to be sent as attachment in weekly email
def download_data(google_workbook):
    print('Exporting to .xlsx')
    google_sheet_list = google_workbook.worksheets()
    today = dt.now().strftime('%Y_%m_%d')

    with xlsxwriter.Workbook(
            os.path.join(base_dir, f'data/SCAN_TPCHD_{today}.xlsx'),
        {'strings_to_numbers': True}) as xlsx_workbook:
        for sheet in google_sheet_list:
            list_of_rows = sheet.get_all_values()
            worksheet = xlsx_workbook.add_worksheet(sheet.title)

            for row_num, data in enumerate(list_of_rows):
                worksheet.write_row(row_num, 0, data)


if __name__ == "__main__":
    main()
