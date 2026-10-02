"""Deterministic synthetic benchmark network for SOLARGRID Tool 08.

This is NOT a Jordanian utility model. It is a small project-owned benchmark
used to make the power-flow contract executable and reproducible.
Requires pandapower at runtime.
"""

NETWORK_ID = "SOLARGRID_DEMO_4BUS_V1"


def build_network():
    import pandapower as pp

    net = pp.create_empty_network(sn_mva=100.0)
    b0 = pp.create_bus(net, vn_kv=33.0, name="GRID_SLACK")
    b1 = pp.create_bus(net, vn_kv=33.0, name="GENERATOR_BUS")
    b2 = pp.create_bus(net, vn_kv=33.0, name="BATTERY_BUS")
    b3 = pp.create_bus(net, vn_kv=33.0, name="LOAD_BUS")

    pp.create_ext_grid(net, bus=b0, vm_pu=1.0, name="Grid reference")
    pp.create_line_from_parameters(net, b0, b1, 1.0, 0.01, 0.04, 0.0, 2.0, name="L01")
    pp.create_line_from_parameters(net, b1, b2, 1.0, 0.01, 0.04, 0.0, 2.0, name="L12")
    pp.create_line_from_parameters(net, b2, b3, 1.0, 0.01, 0.04, 0.0, 2.0, name="L23")

    # Six synthetic dispatch elements mirror the six dataset-backed Jordan
    # project identities. This remains a project-owned 4-bus benchmark, not a
    # model of the real Jordan transmission network.
    g1 = pp.create_sgen(net, b1, p_mw=8.3, q_mvar=0.0, name="Shams Ma'an")
    g2 = pp.create_sgen(net, b1, p_mw=16.2, q_mvar=0.0, name="Quweira")
    g3 = pp.create_sgen(net, b1, p_mw=31.4, q_mvar=0.0, name="Baynouna")
    g4 = pp.create_sgen(net, b2, p_mw=7.9, q_mvar=0.0, name="ACWA Mafraq")
    g5 = pp.create_sgen(net, b2, p_mw=3.1, q_mvar=0.0, name="Falcon Ma'an")
    g6 = pp.create_sgen(net, b2, p_mw=8.1, q_mvar=0.0, name="AM Solar")
    batt = pp.create_storage(net, b2, p_mw=0.0, max_e_mwh=60.0, min_e_mwh=12.0, max_p_mw=20.0, min_p_mw=-25.0, soc_percent=72.0, name="B1")
    load = pp.create_load(net, b3, p_mw=75.0, q_mvar=12.0, name="LOAD_MAIN")

    net.solargrid_mapping = {
        "generators": {"1": int(g1), "2": int(g2), "3": int(g3), "4": int(g4), "5": int(g5), "6": int(g6)},
        "batteries": {"1": int(batt)},
        "loads": {"1": {"element": int(load), "p_mw": 75.0}},
        "voltage_limits_pu": [0.95, 1.05],
        "loading_limit_pct": 100.0,
        "source": "SOLARGRID synthetic benchmark, project-owned",
        "source_type": "synthetic_benchmark",
        "version": "2",
    }
    return net


def load_network_cases():
    try:
        return {NETWORK_ID: build_network()}
    except ImportError:
        return {}
