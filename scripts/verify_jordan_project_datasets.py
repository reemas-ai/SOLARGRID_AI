"""Verify the six Jordan project identities are true dataset-backed assets."""
from __future__ import annotations
import json, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from solar_projects import SOLAR_PROJECTS, EXPECTED_PROJECT_KEYS
from map_status import status_from_availability

def load(name): return json.loads((ROOT/'datasets'/f'{name}.json').read_text(encoding='utf-8'))

def main():
    expected_names=[p['name'] for p in SOLAR_PROJECTS]
    expected_caps=[float(p['capacity_mw']) for p in SOLAR_PROJECTS]
    for scenario in ('normal','problem'):
        data=load(scenario); gens=data['generators']
        assert len(gens)==6, f'{scenario}: expected 6 generators, got {len(gens)}'
        assert [g['name'] for g in gens]==expected_names
        assert [str((g.get('constraints_json') or {}).get('project_key')) for g in gens]==list(EXPECTED_PROJECT_KEYS)
        assert [float(g['capacity_mw']) for g in gens]==expected_caps
        assert abs(sum(float(g['current_output_mw']) for g in gens)-float(data['snapshot']['solar_gen_mw']))<=1e-9
        for g in gens:
            assert float(g['max_output_mw']) <= float(g['capacity_mw']) + 1e-9
            assert status_from_availability(g.get('availability_status'))[0] in {'GREEN','YELLOW','RED'}
        print(f'[{scenario}] six dataset-backed Jordan solar project identities verified; fleet output={data["snapshot"]["solar_gen_mw"]:.1f} MW')
    problem=load('problem')
    colors=[status_from_availability(g['availability_status'])[0] for g in problem['generators']]
    assert 'RED' in colors and 'YELLOW' in colors and 'GREEN' in colors
    print('[problem] GREEN/YELLOW/RED states all represented')
    print('PASS: Jordan project datasets are integrated into the engineering model.')

if __name__=='__main__': main()
