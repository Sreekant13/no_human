"""Cross-VCS review posting: each finding is attributed to the change set that
owns its file, so it lands on the RIGHT PR/MR (the cross-repo case: GHE + 2
GitLab)."""

from no_human.vcs.comment_poster import files_in_diff, pick_pr_for_file

GHE = "https://code.example.com/dev/acme-test/pull/7001"
MR7006 = "https://gitlab.acme.net/ci_gate/customer/metrics-core/-/merge_requests/7006"
MR7007 = "https://gitlab.acme.net/acme-k8s/apps/metrics-core/metrics-core/-/merge_requests/7007"

_PR_FILES = {
    GHE: ["acme-sample-test/src/test/java/com/acme/sample/props/SamplePropsIntegrationIT.java"],
    MR7006: ["workload/backend-service.yaml", "workload/overlays/sample-sync-service/ci_gate/values.yaml.gotmpl"],
    MR7007: ["apps/metrics-core/deploy.yaml"],
}


def test_files_in_diff_extracts_both_grammars():
    diff = (
        "diff --git a/workload/backend-service.yaml b/workload/backend-service.yaml\n"
        "--- a/workload/backend-service.yaml\n+++ b/workload/backend-service.yaml\n@@ -1 +1 @@\n-a\n+b\n"
        "diff --git a/new.txt b/new.txt\n--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1 @@\n+x\n"
    )
    files = files_in_diff(diff)
    assert "workload/backend-service.yaml" in files
    assert "new.txt" in files
    assert "/dev/null" not in files


def test_gitlab_finding_routes_to_its_own_MR_not_the_ghe_pr():
    # backend-service.yaml lives in MR !7006, NOT the acme-test GHE PR — this is the exact
    # bug the user hit (a GitLab finding posted to the GitHub PR).
    assert pick_pr_for_file("workload/backend-service.yaml", _PR_FILES, fallback=GHE) == MR7006
    assert pick_pr_for_file(
        "workload/overlays/sample-sync-service/ci_gate/values.yaml.gotmpl",
        _PR_FILES, fallback=GHE,
    ) == MR7006


def test_ghe_finding_routes_to_the_ghe_pr():
    assert pick_pr_for_file(
        "acme-sample-test/src/test/java/com/acme/sample/props/SamplePropsIntegrationIT.java",
        _PR_FILES, fallback=MR7006,
    ) == GHE


def test_trailing_segment_match_handles_partial_paths():
    # reviewer cited just "backend-service.yaml"; diff has the full path.
    assert pick_pr_for_file("backend-service.yaml", _PR_FILES, fallback=GHE) == MR7006


def test_unknown_file_falls_back_to_the_anchor_pr():
    assert pick_pr_for_file("nowhere/x.go", _PR_FILES, fallback=GHE) == GHE
    assert pick_pr_for_file("", _PR_FILES, fallback=GHE) == GHE
