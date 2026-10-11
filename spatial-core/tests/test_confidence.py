from spatial_core.confidence import derive_confidence


def test_tracking_failure_is_low():
    assert derive_confidence(tracking_normal=False, icp_rmse_m=0.001, object_extent_m=0.5) == "LOW"


def test_good_metrics_high():
    c = derive_confidence(
        icp_rmse_m=0.003,  # 0.6% of 0.5m
        object_extent_m=0.5,
        inlier_ratio=0.92,
        n_points=900,
        depth_valid_ratio=0.85,
    )
    assert c == "HIGH"


def test_poor_metrics_low():
    c = derive_confidence(
        icp_rmse_m=0.04,  # 8% of 0.5m
        object_extent_m=0.5,
        inlier_ratio=0.5,
        n_points=50,
        depth_valid_ratio=0.3,
    )
    assert c == "LOW"


def test_medium_middle_ground():
    c = derive_confidence(
        icp_rmse_m=0.02,  # 4% of 0.5m (나쁨 아님, 좋음 아님)
        object_extent_m=0.5,
        inlier_ratio=0.7,
        n_points=400,
        depth_valid_ratio=0.6,
    )
    assert c == "MEDIUM"


def test_missing_key_metrics_capped_below_high():
    """핵심 지표가 없으면 HIGH가 나오지 않는다 (보수적)."""
    c = derive_confidence(inlier_ratio=0.95, n_points=900)
    assert c != "HIGH"


def test_few_points_caps_confidence():
    c = derive_confidence(
        icp_rmse_m=0.002,
        object_extent_m=0.5,
        inlier_ratio=0.95,
        n_points=60,  # 극소
        depth_valid_ratio=0.9,
    )
    assert c != "HIGH"
