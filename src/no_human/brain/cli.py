"""``nh brain …`` — administration for the team-brain client.

Every subcommand bootstraps with ``require_auth=False``. That is the existing
escape hatch ``nh auth status`` already uses, and here it is a rule rather than
a convenience: ``assert_subscription_mode`` decides which CLAUDE credential
pays, and a brain command must never invoke it. A broken Claude credential must
not block brain administration, and a broken brain must not block anything at
all.

Heavy imports live inside command bodies, matching the ``nh ci-gate`` pattern,
so ``nh --help`` costs nothing.
"""

from __future__ import annotations

import sys

import click
from rich.console import Console

console = Console()


def _cfg():
    from ..cli.commands import _bootstrap
    config, _ = _bootstrap(require_auth=False)
    return config


def _brain(config):
    from .settings import read
    return read(config)


def _require_url(cfg):
    if not cfg.control_plane_url:
        console.print(
            "[red]team_brain.control_plane_url is not set[/] — add it to "
            "~/.no_human/config.yaml:\n"
            "  team_brain:\n"
            "    enabled: true\n"
            "    control_plane_url: https://<your control plane>")
        sys.exit(2)


def _conn(config):
    from . import store
    return store.connect(config.db_path)


@click.group("brain")
def brain_group() -> None:
    """Team brain: shared, admin-approved rules (off by default)."""


@brain_group.command("login")
def brain_login() -> None:
    """Sign in to your team's brain (browser, PKCE, loopback on port 8422)."""
    import secrets
    import time
    import webbrowser

    from . import client, credentials

    config = _cfg()
    cfg = _brain(config)
    _require_url(cfg)

    try:
        auth = client.auth_config(cfg)
    except client.BrainHTTPError as exc:
        console.print(f"[red]cannot start sign-in:[/] {exc}")
        sys.exit(1)

    verifier, challenge = credentials._pkce_pair()
    state = secrets.token_urlsafe(24)
    url = credentials.authorize_url(auth, state, challenge)

    console.print("Opening your browser to sign in. If it does not open, "
                  "visit:\n" + url)
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001 — a headless box still gets the URL above
        pass

    try:
        code = credentials.wait_for_code(state)
        tokens = client.exchange_code(auth, code, verifier,
                                      credentials.LOOPBACK_REDIRECT)
    except (credentials.BrainAuthError, client.BrainHTTPError) as exc:
        console.print(f"[red]sign-in failed:[/] {exc}")
        sys.exit(1)

    claims = credentials.id_token_claims(tokens["id_token"])
    team = str(claims.get("custom:team_id") or "")
    cred = credentials.Credential(
        refresh_token=str(tokens.get("refresh_token") or ""),
        id_token=str(tokens["id_token"]),
        id_token_expires_at=time.time() + float(tokens.get("expires_in") or 3600),
        team_id=team,
        subject=str(claims.get("sub") or ""),
    )
    path = credentials.save(cred)
    console.print(f"[green]signed in[/] to team [bold]{team or '(none)'}[/]; "
                  f"credential stored 0600 at {path}")
    if not team:
        console.print("[yellow]this account carries no team id[/] — an admin "
                      "must assign one before anything will sync.")


@brain_group.command("logout")
def brain_logout() -> None:
    """Forget the stored team-brain credential."""
    from . import credentials
    console.print("[green]credential removed[/]" if credentials.forget()
                  else "no credential was stored")


@brain_group.command("status")
def brain_status() -> None:
    """What this machine holds, and whether it trusts it."""
    import time

    from . import credentials, store
    from .keys import PINNED_FINGERPRINTS

    config = _cfg()
    cfg = _brain(config)
    console.print(f"enabled           : {cfg.enabled}")
    console.print(f"control plane     : {cfg.control_plane_url or '(unset)'}")
    console.print(f"max stale days    : {cfg.max_stale_days}")
    console.print(f"pinned signing key: {', '.join(PINNED_FINGERPRINTS) or '(none)'}")

    cred = credentials.load()
    console.print(f"signed in         : {'yes, team ' + (cred.team_id or '?') if cred else 'no'}")

    conn = _conn(config)
    try:
        trust = store.trust(conn)
        last = store.last_verified_sync(conn)
        rules = store.all_rules(conn)
        usable = [r for r in rules if r.injectable]
        # THE THIRD STATE. `trust` has two values and the machine has three:
        # ok, untrusted, and "held back by the replay guard". The guard does not
        # mark the brain untrusted — every signature in a replayed prefix is
        # genuine — it calls `reset(keep_high_water=True)`, which DELETES the
        # `trust` key, so `store.trust()` returns its `TRUST_OK` default. Status
        # therefore printed `trust: ok`, `watermark: 0`, `last verified sync:
        # never` — a brand-new machine — while nothing was being injected and
        # nothing ever would be again. The operator had no way to see the state
        # they were in, and the one command that exits it was named nowhere they
        # would look: not in status, not in the sync note, only in a source
        # comment. This is the same predicate `brain/__init__.py` injects on.
        high_water = store.chain_high_water(conn)
        chain_top = store.chain_top(conn)
        held_back = chain_top < high_water
        console.print(f"trust             : {trust}"
                      + (f"  ({store.get_state(conn, 'trust_reason', '')})"
                         if trust != store.TRUST_OK else "")
                      + ("  [yellow](held back — see below)[/]"
                         if held_back and trust == store.TRUST_OK else ""))
        console.print(f"watermark         : {store.watermark(conn)}")
        console.print(f"chain verified to : {chain_top}"
                      f"  (high-water mark {high_water})")
        console.print(f"manifests verified: {store.manifest_count(conn)}")
        console.print(f"rules held        : {len(rules)} "
                      f"({len(usable)} injectable, "
                      f"{len(store.blocked_ids(conn))} locally blocked)")
        if last:
            age_days = (time.time() - last) / 86400
            console.print(f"last verified sync: {age_days:.1f} days ago"
                          + ("  [yellow](stale — nothing is injected)[/]"
                             if age_days > cfg.max_stale_days else ""))
        else:
            console.print("last verified sync: never")
        if held_back:
            console.print(
                f"\n[yellow]no remote rule is being injected.[/] The chain this "
                f"machine holds reaches version {chain_top}, before version "
                f"{high_water} which it has already verified — the control "
                f"plane served an older chain than it served before.\n"
                f"  · If the server is catching up, `nh brain sync` clears this "
                f"by itself once it serves version {high_water} again.\n"
                f"  · If this team REBUILT its brain, that will never happen "
                f"and the state is permanent. `nh brain trust --reset` is the "
                f"exit: it forgets the high-water mark and re-verifies the "
                f"whole chain from version 0. Local blocks are kept.")
        if store.get_state(conn, "disabled"):
            console.print("[yellow]locally disabled[/] — no rule is injected "
                          "regardless of config")
        if not cfg.enabled:
            console.print("[dim]team_brain.enabled is false: nothing is "
                          "injected and no prompt differs from a build without "
                          "this feature.[/]")
    finally:
        conn.close()


@brain_group.command("sync")
def brain_sync() -> None:
    """Pull, verify and apply the team's confirmed rules. Explicit only."""
    import time

    from . import client, credentials, store, sync

    config = _cfg()
    cfg = _brain(config)
    _require_url(cfg)

    cred = credentials.load()
    if cred is None:
        console.print("[red]not signed in[/] — run `nh brain login`")
        sys.exit(2)

    id_token = cred.id_token
    if not cred.id_token_valid:
        try:
            auth = client.auth_config(cfg)
            tokens = client.refresh_tokens(auth, cred.refresh_token)
        except client.BrainHTTPError as exc:
            console.print(f"[red]could not refresh the sign-in:[/] {exc}\n"
                          "Run `nh brain login` again.")
            sys.exit(1)
        id_token = str(tokens["id_token"])
        credentials.save(credentials.Credential(
            refresh_token=str(tokens.get("refresh_token") or cred.refresh_token),
            id_token=id_token,
            id_token_expires_at=time.time() + float(tokens.get("expires_in") or 3600),
            team_id=cred.team_id, subject=cred.subject))

    if not cred.team_id:
        console.print("[red]this account carries no team id[/]")
        sys.exit(2)

    try:
        report = sync.run(cfg, id_token, cred.team_id, config.db_path)
    except sync.TrustFailure as exc:
        console.print(f"[bold red]TRUST FAILURE[/] — {exc}\n"
                      "Every remote rule has stopped being injected and will "
                      "not resume on its own. Recover deliberately with "
                      "`nh brain trust --reset`.")
        sys.exit(3)
    except client.BrainHTTPError as exc:
        # Availability, not trust: keep what is verified, change nothing.
        console.print(f"[yellow]sync unavailable:[/] {exc}\n"
                      "Already-verified rules are unchanged; retry later.")
        sys.exit(1)

    console.print(f"[green]synced[/] {report.items} item(s) over "
                  f"{report.pages} page(s); {report.manifests_verified} "
                  f"manifest(s) verified; {report.applied} rule(s) applied; "
                  f"watermark {report.watermark}")
    for rule_id, reason in report.refused[:20]:
        console.print(f"  [dim]refused[/] {rule_id}: {reason}")
    if report.held_aside:
        # Distinct from `refused`, which is a verdict this client reached.
        # These are rows it declined to judge at all because the manifest
        # covering them could not be fetched.
        console.print(f"  [dim]held aside[/] {report.held_aside} row(s) — "
                      "not verified yet; retried on the next sync")
    if report.note:
        console.print(f"[yellow]{report.note}[/]")
    if not cfg.enabled:
        console.print("[dim]team_brain.enabled is false — these rules are "
                      "stored but nothing injects them.[/]")


@brain_group.command("list")
def brain_list() -> None:
    """Every rule held, its provenance, and why anything was refused."""
    from . import store

    config = _cfg()
    conn = _conn(config)
    try:
        rules = store.all_rules(conn)
        blocked = set(store.blocked_ids(conn))
        if not rules:
            console.print("no remote rules held")
            return
        for rule in rules:
            if rule.rule_id in blocked:
                mark, why = "[red]blocked[/]", "locally blocked"
            elif rule.injectable:
                mark, why = "[green]inject[/]", f"manifest {rule.manifest_version}"
            else:
                mark, why = "[yellow]held[/]  ", rule.refused_reason or "?"
            console.print(f"{mark} {rule.rule_id} v{rule.version} "
                          f"[{rule.visibility}] {rule.title}  [dim]({why})[/]")
    finally:
        conn.close()


@brain_group.command("block")
@click.argument("rule_id")
def brain_block(rule_id: str) -> None:
    """Permanently exclude a rule on THIS machine. Sync cannot clear it."""
    from . import store

    config = _cfg()
    conn = _conn(config)
    try:
        store.block(conn, rule_id)
        console.print(f"[green]blocked[/] {rule_id} — it will never be "
                      "injected on this machine, whatever the team admits.")
    finally:
        conn.close()


@brain_group.command("disable")
def brain_disable() -> None:
    """Stop injecting every remote rule, now, without contacting the service."""
    from . import store

    config = _cfg()
    conn = _conn(config)
    try:
        store.set_state(conn, "disabled", "1")
        console.print("[green]disabled[/] — no remote rule will be injected. "
                      "This flag is local and a sync cannot clear it. Set "
                      "team_brain.enabled: false in config.yaml to also stop "
                      "the package from being imported at all.")
    finally:
        conn.close()


@brain_group.command("trust")
@click.option("--reset", is_flag=True,
              help="Discard all synced state and re-verify the chain from "
                   "version 0. The ONLY recovery from a trust failure.")
def brain_trust(reset: bool) -> None:
    """Inspect or reset the trust state."""
    from . import store

    config = _cfg()
    conn = _conn(config)
    try:
        if not reset:
            console.print(f"trust: {store.trust(conn)}"
                          f"  {store.get_state(conn, 'trust_reason', '') or ''}")
            return
        store.reset(conn)
        store.set_state(conn, "trust", store.TRUST_OK)
        # `disabled` is deliberately NOT cleared. A trust reset is the operator
        # recovering from a signature failure; it is not the operator changing
        # their mind about having switched the feature off.
        console.print("[green]trust reset[/] — all synced state discarded. "
                      "Run `nh brain sync` to refetch and re-verify the whole "
                      "chain from version 0. Local blocks were kept.")
    finally:
        conn.close()
