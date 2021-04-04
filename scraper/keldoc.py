import time
from datetime import datetime, timedelta
from urllib.parse import urlsplit, parse_qs

import requests
from requests.adapters import HTTPAdapter
from urllib3 import Retry

session = requests.Session()

retries = Retry(total=2,
                backoff_factor=0.1,
                status_forcelist=[500, 502, 503, 504])
session.mount("https://", HTTPAdapter(max_retries=retries))

KELDOC_COVID_SPECIALTIES = [
    'Maladies infectieuses'
]

KELDOC_APPOINTMENT_REASON = [
    '1ère injection'
]

API_KELDOC_CABINETS = 'https://booking.keldoc.com/api/patients/v2/clinics/{0}/specialties/{1}/cabinets'

class KeldocCenter:

    def __init__(self, base_url):
        self.base_url = base_url
        self.resource_params = None
        self.id = None
        self.specialties = None
        self.vaccine_specialties = None
        self.vaccine_cabinets = None
        self.vaccine_motives = None

    def filter_vaccine_specialties(self):
        if not self.specialties:
            return False
        # Put relevant specialty & cabinet IDs in lists
        self.vaccine_specialties = []
        for specialty in self.specialties:
            if not is_specialty_relevant(specialty.get('name', None)):
                print(specialty)
                continue
            self.vaccine_specialties.append(specialty.get('id', None))
        return self.vaccine_specialties

    def filter_vaccine_motives(self):
        if not self.id or not self.vaccine_specialties or not self.vaccine_cabinets:
            return None

        motive_url = 'https://booking.keldoc.com/api/patients/v2/clinics/{0}/specialties/{1}/cabinets/{2}/motive_categories'
        motive_categories = []
        self.vaccine_motives = []

        for specialty in self.vaccine_specialties:
            for cabinet in self.vaccine_cabinets:
                try:
                    motive_req = session.get(motive_url.format(self.id, specialty, cabinet), timeout=10)
                except requests.exceptions.Timeout:
                    continue
                motive_req.raise_for_status()
                motive_data = motive_req.json()
                motive_categories.extend(motive_data)

        for motive_cat in motive_categories:
            motives = motive_cat.get('motives', {})
            for motive in motives:
                motive_name = motive.get('name', None)

                if not motive_name or not is_appointment_relevant(motive_name):
                    continue
                motive_agendas = [motive_agenda.get('id', None) for motive_agenda in motive.get('agendas', {})]
                motive_info = {
                    'id': motive.get('id', None),
                    'agendas': motive_agendas
                }
                self.vaccine_motives.append(motive_info)
        return self.vaccine_motives

    def fetch_vaccine_cabinets(self):
        if not self.id or not self.vaccine_specialties:
            return False

        self.vaccine_cabinets = []
        for specialty in self.vaccine_specialties:
            cabinet_url = API_KELDOC_CABINETS.format(self.id, specialty)
            try:
                cabinet_req = session.get(cabinet_url, timeout=5)
            except requests.exceptions.Timeout:
                continue
            cabinet_req.raise_for_status()
            data = cabinet_req.json()
            if not data:
                continue
            for cabinet in data:
                cabinet_id = cabinet.get('id', None)
                if not cabinet_id:
                    continue
                self.vaccine_cabinets.append(cabinet_id)
        return self.vaccine_cabinets

    def fetch_center_data(self):
        if not self.base_url:
            return False
        # Fetch center id
        resource_url = f'https://booking.keldoc.com/api/patients/v2/searches/resource'
        try:
            resource = session.get(resource_url, params=self.resource_params, timeout=10)
        except requests.exceptions.Timeout:
            return False
        resource.raise_for_status()
        data = resource.json()

        self.id = data.get('id', None)
        self.specialties = data.get('specialties', None)
        return True

    def parse_resource(self):
        if not self.base_url:
            return False

        # Fetch new URL after redirection
        try:
            rq = session.get(self.base_url, timeout=10)
        except requests.exceptions.Timeout:
            return False
        rq.raise_for_status()
        new_url = rq.url

        # Parse relevant GET params for Keldoc API requests
        query = urlsplit(new_url).query
        params_get = parse_qs(query)
        mandatory_params = ['dom', 'inst', 'user']
        # Some vaccination centers on Keldoc do not
        # accept online appointments, so you cannot retrieve data
        for mandatory_param in mandatory_params:
            if not mandatory_param in params_get:
                return False
        self.resource_params = {
            'type': params_get.get('dom')[0],
            'location': params_get.get('inst')[0],
            'slug': params_get.get('user')[0]
        }
        return True

    def find_first_availability(self, start_date, end_date):
        if not self.vaccine_motives:
            return None

        # Find next availabilities
        first_availability = None
        for relevant_motive in self.vaccine_motives:
            if not 'id' in relevant_motive or not 'agendas' in relevant_motive:
                continue
            motive_id = relevant_motive.get('id', None)
            calendar_url = f'https://www.keldoc.com/api/patients/v2/timetables/{motive_id}'
            calendar_params = {
                'from': start_date,
                'to': end_date,
                'agenda_ids[]': relevant_motive.get('agendas', [])
            }
            try:
                calendar_req = session.get(calendar_url, params=calendar_params, timeout=10)
            except requests.exceptions.Timeout:
                # Some requests on Keldoc are taking too much time (for few centers)
                # and block the process completion.
                continue
            calendar_req.raise_for_status()

            date = parse_keldoc_availability(calendar_req.json())
            if date is None:
                continue
            # Compare first available date
            if first_availability is None or date < first_availability:
                first_availability = date
        return first_availability


# Filter by relevant appointments
def is_appointment_relevant(appointment_name):
    if not appointment_name:
        return False

    appointment_name = appointment_name.lower()
    for allowed_appointments in KELDOC_APPOINTMENT_REASON:
        if allowed_appointments in appointment_name:
            return True
    return False


# Filter by relevant specialties
def is_specialty_relevant(specialty_name):
    if not specialty_name:
        return False

    for allowed_specialties in KELDOC_COVID_SPECIALTIES:
        if allowed_specialties == specialty_name:
            return True
    return False


# Get relevant cabinets
def get_relevant_cabinets(center_id, specialty_ids):
    cabinets = []
    if not center_id or not specialty_ids:
        return cabinets

    for specialty in specialty_ids:
        cabinet_url = f'https://booking.keldoc.com/api/patients/v2/clinics/{center_id}/specialties/{specialty}/cabinets'
        try:
            cabinet_req = session.get(cabinet_url, timeout=5)
        except requests.exceptions.Timeout:
            continue
        cabinet_req.raise_for_status()
        data = cabinet_req.json()
        if not data:
            return cabinets
        for cabinet in data:
            cabinet_id = cabinet.get('id', None)
            if not cabinet_id:
                continue
            cabinets.append(cabinet_id)
    return cabinets

def parse_keldoc_availability(availability_data):
    if not availability_data:
        return None
    if 'date' in availability_data:
        date = availability_data.get('date', None)
        date_obj = datetime.strptime(date, '%Y-%m-%dT%H:%M:%S.%f%z')
        return date_obj

    availabilities = availability_data.get('availabilities', None)
    if availabilities is None:
        return None
    for date in availabilities:
        slots = availabilities.get(date, [])
        if not slots:
            continue
        for slot in slots:
            start_date = slot.get('start_time', None)
            if not start_date:
                continue
            return datetime.strptime(start_date, '%Y-%m-%dT%H:%M:%S.%f%z')
    return None


def fetch_slots(base_url, start_date):
    # Keldoc needs an end date, but if no appointment are found,
    # it still returns the next available appointment. Bigger end date
    # makes Keldoc responses slower.
    date_obj = datetime.strptime(start_date, '%Y-%m-%d')
    end_date = (date_obj + timedelta(days=1)).strftime('%Y-%m-%d')

    center = KeldocCenter(base_url=base_url)
    # Unable to parse center resources (id, location)?
    if not center.parse_resource():
        return None
    # Try to fetch center data
    if not center.fetch_center_data():
        return None

    # Filter specialties, cabinets & motives
    center.filter_vaccine_specialties()
    center.fetch_vaccine_cabinets()
    center.filter_vaccine_motives()
    # Find the first availability
    return center.find_first_availability(start_date, end_date)