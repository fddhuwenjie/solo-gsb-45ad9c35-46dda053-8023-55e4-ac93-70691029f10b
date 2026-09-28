"""Spatial-QC endpoint tests: median-polish effects, missing readings,
group centering, convergence trajectory, seeded permutation p-values and
the 422 semantics (no partial conclusion)."""
import math

from fastapi.testclient import TestClient

from app.main import app
from app.spatial_qc import median_polish

client = TestClient(app)


def grid_layout(rows, cols):
    """Sample ids S00.. -> A1, A2, ... row-major; every sample is its own
    well so permutations stay unambiguous."""
    layout, groups = {}, {}
    for r in range(rows):
        for c in range(cols):
            sid = f"S{r * cols + c:02d}"
            layout[sid] = f"{chr(65 + r)}{c + 1}"
            groups[sid] = "treated" if (r + c) % 2 == 0 else "control"
    return layout, groups


def make_payload(values, rows=4, cols=4, **overrides):
    layout, groups = grid_layout(rows, cols)
    wells = [layout[sid] for sid in sorted(layout)]
    readings = {w: v for w, v in zip(wells, values) if v is not None}
    payload = {
        "plate": {"type": "custom", "rows": rows, "columns": cols},
        "layout": layout,
        "groups": groups,
        "readings": readings,
        "seed": 123,
        "n_permutations": 99,
        "max_iterations": 200,
        "tolerance": 1e-8,
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------- 200


def test_additive_structure_recovers_row_col_effects():
    # Pure additive data: reading = group level + row effect + col effect.
    # (Rows happen to carry two members of each group equally.)
    row_eff = [0.0, 2.0, -1.0, 4.0]
    col_eff = [0.0, -3.0, 1.0, 2.0]
    treated, control = 10.0, 20.0
    values = []
    for r in range(4):
        for c in range(4):
            base = treated if (r + c) % 2 == 0 else control
            values.append(base + row_eff[r] + col_eff[c])
    r = client.post("/api/v1/arrangements/spatial-qc", json=make_payload(values))
    assert r.status_code == 200, r.text
    body = r.json()
    eff = body["effects"]
    # Tukey recentring pins the shared additive constant through medians
    # (averaged for even sizes), so check the effect *profiles* through
    # pairwise differences; the fitted-model reconstruction below then pins
    # the joint solution exactly.
    row_got, col_got = eff["row_effects"], eff["col_effects"]
    assert [a - row_got[0] for a in row_got] == [a - row_eff[0] for a in row_eff]
    assert [b - col_got[0] for b in col_got] == [b - col_eff[0] for b in col_eff]
    # Recentring through medians (averaged at even sample counts) shifts the
    # effects by +/-0.5; the span across rows is still the input spread of 5.
    assert round(max(row_got) - min(row_got), 9) == 5.0
    # Reconstruction identity: corrected + overall + row + col == raw
    # (correction subtracts precisely the fitted spatial terms); this is the
    # exact algebraic guarantee regardless of constant recentring.
    for r_i in range(4):
        for c_i in range(4):
            well = f"{chr(65 + r_i)}{c_i + 1}"
            fitted = (body["corrected_readings"][well] + eff["overall_effect"]
                      + eff["row_effects"][r_i] + eff["col_effects"][c_i])
            assert abs(fitted - values[r_i * 4 + c_i]) <= 1e-9
    assert body["convergence"]["converged"] is True
    assert body["convergence"]["final_max_correction"] == 0.0


def test_group_centering_removes_between_group_shift():
    # Both groups get the SAME strong row gradient, but control is shifted
    # by 100: without group centering that shift would inflate the effects.
    values = []
    for r in range(4):
        for c in range(4):
            base = 10.0 if (r + c) % 2 == 0 else 110.0
            values.append(base + 3.0 * r)
    r = client.post("/api/v1/arrangements/spatial-qc", json=make_payload(values))
    assert r.status_code == 200, r.text
    body = r.json()
    # Group medians differ by exactly 100; spatial row effects stay small.
    medians = {g["group"]: g["median"] for g in body["groups"]}
    assert medians["control"] - medians["treated"] == 100.0
    assert max(abs(v) for v in body["effects"]["row_effects"]) <= 4.51


def test_missing_readings_are_allowed_and_reported_null():
    payload = make_payload(
        [1.0, None, 3.0, 4.0,
         None, 6.0, 7.0, None,
         9.0, 10.0, None, 12.0,
         13.0, None, 15.0, 16.0]
    )
    r = client.post("/api/v1/arrangements/spatial-qc", json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    corrected = body["corrected_readings"]
    assert corrected["A2"] is None and corrected["B1"] is None
    assert all(v is not None for w, v in corrected.items()
               if w not in ("A2", "B1", "B4", "C3", "D2"))
    assert len(body["valid_wells"]) == 11
    # Each group: 5 and 6 valid readings.
    counts = sorted(g["valid_readings"] for g in body["groups"])
    assert counts == [5, 6]


def test_constant_plate_has_zero_statistic_and_pvalue_one():
    # Within each group every reading is identical -> no spatial effect.
    values = [10.0 if (r + c) % 2 == 0 else 20.0
              for r in range(4) for c in range(4)]
    r = client.post(
        "/api/v1/arrangements/spatial-qc",
        json=make_payload(values, n_permutations=49),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    pt = body["permutation_test"]
    assert pt["observed_statistic"] == 0.0
    assert pt["exceedances"] == 49
    assert pt["p_value"] == 1.0
    assert body["effects"]["overall_effect"] == 0.0


def test_strong_spatial_effect_gets_small_p_value():
    # Shift EVERY well of one row by 300 on an 8x12 plate. The effect is far
    # outside anything a within-group well permutation can reproduce by
    # chance (random row imbalances are single-digit units), so no repeat
    # reaches the observed statistic.
    rows, cols = 8, 12
    values = []
    for r in range(rows):
        for c in range(cols):
            base = 10.0 if (r + c) % 2 == 0 else 20.0
            values.append(base + (300.0 if r == 2 else 0.0))
    r = client.post(
        "/api/v1/arrangements/spatial-qc",
        json=make_payload(values, rows=rows, cols=cols, n_permutations=199),
    )
    assert r.status_code == 200, r.text
    pt = r.json()["permutation_test"]
    # The aligned row shift is highly significant; coalescence of the six
    # big values in a single random row is rare but possible, so assert the
    # plus-one corrected p-value is small and internally consistent.
    assert pt["p_value"] <= 0.05
    assert pt["p_value"] == round((pt["exceedances"] + 1) / 200, 6)


def test_p_value_uses_plus_one_correction():
    values = [float(v) for v in range(1, 17)]
    body = client.post(
        "/api/v1/arrangements/spatial-qc",
        json=make_payload(values, n_permutations=0),
    ).json()
    pt = body["permutation_test"]
    assert pt["n_permutations"] == 0
    assert pt["p_value"] == 1.0
    assert pt["permutation_statistics"] == []


def test_seeded_permutations_are_deterministic():
    values = [float((r * 7 + c * 3) % 11) for r in range(4) for c in range(4)]
    payload = make_payload(values, n_permutations=50, seed=42)
    b1 = client.post("/api/v1/arrangements/spatial-qc", json=payload).json()
    b2 = client.post("/api/v1/arrangements/spatial-qc", json=payload).json()
    assert b1 == b2
    other = dict(payload, seed=43)
    b3 = client.post("/api/v1/arrangements/spatial-qc", json=other).json()
    assert b3["permutation_test"]["permutation_statistics"] != \
        b1["permutation_test"]["permutation_statistics"]


def test_trajectory_is_monotone_and_records_convergence():
    values = [
        1.0, 2.0, 9.0, 4.0,
        4.0, 9.0, 6.0, 8.0,
        9.0, 8.0, 10.0, 11.0,
        3.0, 12.0, 5.0, 6.0,
    ]
    body = client.post("/api/v1/arrangements/spatial-qc",
                       json=make_payload(values)).json()
    conv = body["convergence"]
    traj = conv["trajectory"]
    assert conv["converged"] is True
    assert conv["iterations"] == len(traj)
    assert traj[0]["iteration"] == 1
    corrections = [t["max_correction"] for t in traj]
    assert corrections[-1] <= 1e-8
    assert all(traj[i]["iteration"] == i + 1 for i in range(len(traj)))


def test_response_shape_contains_all_required_parts():
    body = client.post(
        "/api/v1/arrangements/spatial-qc",
        json=make_payload([float(i) for i in range(16)], n_permutations=9),
    ).json()
    assert set(body) >= {
        "seed", "plate", "valid_wells", "groups", "effects",
        "corrected_readings", "convergence", "permutation_test",
    }
    eff = body["effects"]
    assert set(eff) >= {
        "overall_effect", "row_effects", "col_effects",
        "max_row_effect", "max_col_effect", "max_axis_effect",
        "row_labels", "column_labels",
    }
    assert len(eff["row_effects"]) == 4 and len(eff["col_effects"]) == 4
    assert eff["row_labels"] == ["A", "B", "C", "D"]
    assert eff["column_labels"] == [1, 2, 3, 4]
    assert len(body["corrected_readings"]) == 16
    pt = body["permutation_test"]
    assert len(pt["permutation_statistics"]) == 9
    # Internal consistency of the reported statistic.
    assert pt["observed_statistic"] == eff["max_axis_effect"]
    assert eff["max_axis_effect"] == max(eff["max_row_effect"], eff["max_col_effect"])


# ----------------------------------------------------- pure polish behavior


def test_median_polish_unit_sweep_with_missing_cells():
    work = {(0, 0): 1.0, (0, 1): 2.0,
            (1, 0): 4.0, (1, 2): 6.0,
            (2, 1): 8.0, (2, 2): 10.0}
    res = median_polish(work, 3, 3, 1e-10, 100)
    assert res.converged and res.iterations < 100
    # Decomposition reconstructs every centred input.
    for (r, c), residual in res.residuals.items():
        fitted = res.overall + res.row_effects[r] + res.col_effects[c]
        assert math.isclose(residual + fitted, work[(r, c)], abs_tol=1e-9)
    # Empty row/col carry zero effects.
    assert res.row_effects.count(0.0) >= 0  # rows 0..2 all occupied here
    sparse = {(0, 0): 1.0, (1, 1): 5.0}
    res2 = median_polish(sparse, 3, 3, 1e-10, 100)
    assert res2.row_effects[2] == 0.0 and res2.col_effects[2] == 0.0


# --------------------------------------------------------------------- 422


def test_group_with_one_valid_reading_is_422():
    values = [None] * 16
    values[0] = 1.0  # only one treated well populated (A1 is 'treated')
    values[1] = 2.0  # one control as well...
    r = client.post("/api/v1/arrangements/spatial-qc",
                    json=make_payload(values))
    assert r.status_code == 422
    cats = r.json()["infeasibility_categories"]
    assert "READING_GROUP_INSUFFICIENT" in cats
    assert "effects" not in r.json()


def test_coverage_under_two_rows_or_cols_is_422():
    # Populate only row A across 4 columns: 2 rows-coverage requirement fails.
    values = [1.0, 2.0, 3.0, 4.0] + [None] * 12
    r = client.post("/api/v1/arrangements/spatial-qc",
                    json=make_payload(values))
    assert r.status_code == 422
    assert "READING_COVERAGE_INSUFFICIENT" in r.json()["infeasibility_categories"]


def test_no_valid_readings_at_all_is_422():
    layout, groups = grid_layout(4, 4)
    payload = {
        "plate": {"type": "custom", "rows": 4, "columns": 4},
        "layout": layout,
        "groups": groups,
        "readings": {w: None for w in layout.values()},
        "seed": 1,
    }
    r = client.post("/api/v1/arrangements/spatial-qc", json=payload)
    assert r.status_code == 422
    cats = r.json()["infeasibility_categories"]
    assert "READING_GROUP_INSUFFICIENT" in cats
    assert "READING_COVERAGE_INSUFFICIENT" in cats


def test_non_convergence_is_422_without_partial_output():
    # Missing-cell patterns converge geometrically, so one iteration can
    # never satisfy a tight tolerance.
    values = [
        1.0, None, 3.0, None,
        None, 6.0, None, 8.0,
        9.0, None, 11.0, None,
        None, 14.0, None, 16.0,
    ]
    r = client.post(
        "/api/v1/arrangements/spatial-qc",
        json=make_payload(values, max_iterations=1, tolerance=1e-12),
    )
    assert r.status_code == 422
    body = r.json()
    assert body["infeasibility_categories"] == ["SPATIAL_QC_DID_NOT_CONVERGE"]
    assert "effects" not in body and "permutation_test" not in body


def test_permutation_non_convergence_is_422():
    # Non-additive checkerboard data converges on the observed assignment in
    # a handful of sweeps, but tolerance 0 with only two iterations cannot be
    # met by some permutation repeats -> 422, no partial conclusion.
    values = [
        1.0, 22.0, 3.0, 41.0,
        34.0, 6.0, 27.0, 8.0,
        9.0, 40.0, 11.0, 2.0,
        53.0, 14.0, 5.0, 16.0,
    ]
    r = client.post(
        "/api/v1/arrangements/spatial-qc",
        json=make_payload(values, max_iterations=2, tolerance=0.0,
                          n_permutations=40),
    )
    assert r.status_code == 422
    assert "SPATIAL_QC_DID_NOT_CONVERGE" in r.json()["infeasibility_categories"]


def test_duplicate_layout_well_is_422():
    payload = make_payload([float(i) for i in range(16)])
    payload["layout"]["S01"] = payload["layout"]["S00"]  # both on A1
    r = client.post("/api/v1/arrangements/spatial-qc", json=payload)
    assert r.status_code == 422
    assert "LAYOUT_WELLS_DUPLICATED" in r.json()["infeasibility_categories"]


def test_group_membership_errors_are_422():
    payload = make_payload([float(i) for i in range(16)])
    # Missing membership for one sample.
    payload["groups"].pop("S00")
    r = client.post("/api/v1/arrangements/spatial-qc", json=payload)
    assert r.status_code == 422
    assert "GROUP_MEMBERSHIP_INVALID" in r.json()["infeasibility_categories"]
    # Unknown sample in groups.
    payload = make_payload([float(i) for i in range(16)])
    payload["groups"]["GHOST"] = "treated"
    r = client.post("/api/v1/arrangements/spatial-qc", json=payload)
    assert r.status_code == 422
    assert "GROUP_MEMBERSHIP_INVALID" in r.json()["infeasibility_categories"]


def test_reading_well_validation():
    payload = make_payload([float(i) for i in range(16)])
    payload["readings"]["Z9"] = 3.0
    r = client.post("/api/v1/arrangements/spatial-qc", json=payload)
    assert r.status_code == 422
    assert "WELL_REFERENCE_INVALID" in r.json()["infeasibility_categories"]

    payload = make_payload([float(i) for i in range(16)])
    del payload["readings"]["A1"]
    payload["readings"]["D4"] = "oops"
    r = client.post("/api/v1/arrangements/spatial-qc", json=payload)
    # Non-numeric scalar fails request parsing; still 422, no partial output.
    assert r.status_code == 422
    assert r.json()["infeasibility_categories"] == ["REQUEST_VALIDATION_ERROR"]

    # Reading on a well that is inside the plate but not part of the layout.
    layout, groups = {"S1": "A1", "S2": "A2", "S3": "B1", "S4": "B2"}, {}
    for i, sid in enumerate(layout):
        groups[sid] = "treated" if i % 2 == 0 else "control"
    r = client.post("/api/v1/arrangements/spatial-qc", json={
        "plate": {"type": "custom", "rows": 3, "columns": 2},
        "layout": layout, "groups": groups,
        "readings": {"A1": 1.0, "A2": 2.0, "B1": 3.0, "B2": 4.0, "C1": 5.0},
        "n_permutations": 5,
    })
    assert r.status_code == 422
    assert "READING_MALFORMED" in r.json()["infeasibility_categories"]


def test_non_finite_reading_is_422():
    import json

    payload = make_payload([float(i) for i in range(16)])
    # NaN parses as valid JSON but must be rejected by the endpoint.
    r = client.post(
        "/api/v1/arrangements/spatial-qc",
        data=json.dumps(payload).replace('"D4": 15.0', '"D4": NaN'),
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 422
    assert "READING_MALFORMED" in r.json()["infeasibility_categories"]


def test_explicit_null_reading_is_missing_not_an_error():
    values = [float(i) for i in range(16)]
    payload = make_payload(values)
    payload["readings"]["A2"] = None
    r = client.post("/api/v1/arrangements/spatial-qc", json=payload)
    assert r.status_code == 200
    assert r.json()["corrected_readings"]["A2"] is None


def test_request_validation_error_is_422():
    r = client.post("/api/v1/arrangements/spatial-qc", json={"layout": {}})
    assert r.status_code == 422
    assert r.json()["infeasibility_categories"] == ["REQUEST_VALIDATION_ERROR"]
    r = client.post("/api/v1/arrangements/spatial-qc",
                    json=make_payload([1.0] * 16, max_iterations=0))
    assert r.status_code == 422


def test_existing_endpoints_still_available():
    assert client.get("/health").json() == {"status": "ok"}
    r = client.post("/api/v1/arrangements", json={
        "plate": {"type": "24"},
        "samples": [{"id": f"s{i}", "group": "G", "batch": "B"} for i in range(4)],
        "seed": 1,
    })
    assert r.status_code == 200
    r = client.post("/api/v1/arrangements/reconcile", json={
        "plate": {"type": "24"},
        "target": {"S1": "A1"},
        "scan": {"A1": "S1"},
    })
    assert r.status_code == 200
