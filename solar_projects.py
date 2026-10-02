"""Jordan solar-project identity metadata used by the SolarGrid demo.

Project identities/capacities are reference metadata. Runtime output, availability,
reserve and dispatch values in the shipped datasets are synthetic demo values and
must not be interpreted as live utility telemetry.
"""
from __future__ import annotations

SOLAR_PROJECTS = [{'project_key': 'SHAMS_MAAN', 'name': "Shams Ma'an Solar Power Plant", 'location': "Ma'an Development Area", 'governorate': "Ma'an", 'lat': 30.197, 'lon': 35.734, 'capacity_mw': 52.5, 'source': "Shams Ma'an Power Generation Company / Jordan MEMR"}, {'project_key': 'QUWEIRA', 'name': 'Quweira Solar Power Plant', 'location': 'Quweira', 'governorate': 'Aqaba', 'lat': 29.8, 'lon': 35.31, 'capacity_mw': 103.0, 'source': 'Jordan Ministry of Energy and Mineral Resources'}, {'project_key': 'BAYNOUNA', 'name': 'Baynouna Solar Energy Project', 'location': 'Muwaqqar / East Amman', 'governorate': 'Amman', 'lat': 31.81, 'lon': 36.15, 'capacity_mw': 200.0, 'source': 'Masdar'}, {'project_key': 'ACWA_MAFRAQ', 'name': 'ACWA Sunrise Al Mafraq Solar PV', 'location': 'King Hussein Bin Talal Development Area', 'governorate': 'Mafraq', 'lat': 32.27, 'lon': 36.19, 'capacity_mw': 50.0, 'source': 'EBRD project record'}, {'project_key': 'FALCON_MAAN', 'name': "Falcon Ma'an Solar Project", 'location': "Ma'an Development Area", 'governorate': "Ma'an", 'lat': 30.235, 'lon': 35.77, 'capacity_mw': 20.0, 'source': 'Jordan MEMR renewable-energy project list'}, {'project_key': 'AM_SOLAR', 'name': 'AM Solar Project', 'location': 'Madounah / East Amman', 'governorate': 'Amman', 'lat': 31.89, 'lon': 36.115, 'capacity_mw': 52.0, 'source': 'Jordan Ministry of Energy and Mineral Resources'}]
SOLAR_PROJECTS_BY_KEY = {row["project_key"]: row for row in SOLAR_PROJECTS}
SOLAR_PROJECTS_BY_NAME = {row["name"]: row for row in SOLAR_PROJECTS}
EXPECTED_PROJECT_KEYS = tuple(row["project_key"] for row in SOLAR_PROJECTS)
