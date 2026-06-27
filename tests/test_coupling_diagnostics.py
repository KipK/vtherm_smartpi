from custom_components.vtherm_smartpi.smartpi.diagnostics import _build_coupling_block


class _Algo:
    _last_coupling_diag = {
        "any_door_open": True,
        "b_base": 0.008,
        "b_eff": 0.07,
        "text_eff": 7.0,
        "sum_k": 0.062,
        "open_neighbors": ["win"],
        "component_power_w": 0.0,
        "edges": {"win": {"coeff": 0.02, "var": 0.01, "reliable": True, "n": 30,
                          "kind": "outside"}},
    }


def test_coupling_block_exposes_edges_and_network():
    block = _build_coupling_block(_Algo())
    assert block["any_aperture_open"] is True
    assert block["b_eff"] == 0.07
    assert block["edges"]["win"]["kind"] == "outside"
    assert block["edges"]["win"]["reliable"] is True
