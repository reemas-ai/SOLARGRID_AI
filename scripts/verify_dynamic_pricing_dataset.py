"""Pure-JSON regression for the optional Dynamic Pricing demo dataset."""
from __future__ import annotations
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
data=json.loads((ROOT/'datasets'/'dynamic_pricing.json').read_text(encoding='utf-8'))
assert data['scenario']=='dynamic_pricing'
assert float(data['snapshot']['other_gen_mw'])==0.0
assert float(data['snapshot']['solar_gen_mw'])==sum(float(g['current_output_mw']) for g in data['generators'])
assert len(data['generators'])==6
assert all(str((g.get('constraints_json') or {}).get('technology','')).upper()=='SOLAR' for g in data['generators'])
assert any(bool(x.get('is_flexible')) and float(x.get('p_mw',0))>0 for x in data['loads'])
assert all('wind' not in json.dumps(row).lower() for row in data['forecasts'])
by={int(x['target_offset_minutes']):x for x in data['forecasts']}
assert set(by)==set(range(0,121,15))
# With battery at its configured max SOC, Tool17's approved demo export capacity
# of 60 MW makes offsets 30 and 60 explicit solar-surplus opportunities.
assert float(data['batteries'][0]['soc_pct'])==float(data['batteries'][0]['max_soc_pct'])
assert float(by[30]['solar_mw']) > float(by[30]['demand_mw']) + 60.0
assert float(by[60]['solar_mw']) > float(by[60]['demand_mw']) + 60.0
print('PASS: optional dynamic_pricing dataset is solar-only, flexible-load enabled, and contains forecast surplus intervals.')
