"""Verify normal execution variability cannot by itself exceed grid-balance tolerance."""
from __future__ import annotations
import json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def main():
    policy=json.loads((ROOT/'config/decision_policy_v1.json').read_text())
    sim=json.loads((ROOT/'config/simulation_v1.json').read_text())
    normal=json.loads((ROOT/'datasets/normal.json').read_text())
    cfg=sim['execution_variability']
    tolerance=float(policy['hard_constraints']['residual_imbalance_tolerance_mw'])
    tracking=float(policy['command_tracking']['tolerance_mw'])
    gen_delta=float(cfg['generator_max_abs_mw'])
    bat_delta=float(cfg['battery_max_abs_mw'])
    max_available_gens=sum(1 for g in normal['generators'] if str(g['availability_status']).upper()=='AVAILABLE')
    max_available_bats=sum(1 for b in normal['batteries'] if str(b['availability_status']).upper()=='AVAILABLE')
    worst=max_available_gens*gen_delta+max_available_bats*bat_delta
    assert gen_delta < tracking and bat_delta < tracking
    assert worst < tolerance, f'worst-case variability {worst} must be < residual tolerance {tolerance}'
    print(f'Available resources: {max_available_gens} generators + {max_available_bats} battery')
    print(f'Worst-case aggregate normal variability budget: {worst:.3f} MW < {tolerance:.3f} MW tolerance')
    print('PASS: normal variability cannot by itself force a goal-verification failure.')

if __name__=='__main__': main()
