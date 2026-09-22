#!/usr/bin/env python3
"""Rename German identifiers to English, in identifiers only.

This repository is published. Its documents are English and the inside of its
code was not: a reader of the public tree meets `pruefe`, `zeile`, `wurzel` and
has to guess (O182). The rename is mechanical and the suite is the oracle, but
two things make a naive `sed` the wrong instrument:

* **A German word in a string is not an identifier.** The ledger is German and
  internal, and several checks read it -- `"getracktes Todo"`, `"Priorität"`,
  the fixtures in `test_source_hygiene.py`. Rewriting those changes behaviour
  while looking like a rename. So this walks tokens and edits `NAME` tokens
  only; strings, comments and docstrings come out byte-identical.
* **One German word is two English ones.** `zeile` is a board *row* in
  `readiness.py` and a *line* of output in `telemetry.py`. A single global
  choice would be wrong in one of them, so the mapping takes per-file
  overrides and the default map carries only words that mean one thing here.

What it deliberately does not do: translate prose. A German comment is a
different job with a different risk, and mixing the two would put a judgement
call in a commit whose whole value is that it contains none.

Usage:
    python3 tools/identifiers.py check [PATH ...]
    python3 tools/identifiers.py plan PATH
    python3 tools/identifiers.py apply PATH ...
"""
from __future__ import annotations

import argparse
import ast
import io
import re
import tokenize
from pathlib import Path

HOH = Path(__file__).resolve().parent.parent

#: Words that mean exactly one thing in this repository. Anything ambiguous
#: belongs in OVERRIDES, beside the file that decides it.
DEFAULT: dict[str, str] = {
    # nouns
    "wurzel": "root", "wurzeln": "roots",
    "pfad": "path", "pfade": "paths", "pfad_fuer": "path_for",
    "datei": "file", "dateien": "files", "dateiname": "filename",
    "dateimenge": "file_set",
    "quelle": "source", "quellen": "sources", "quellencheck": "source_check",
    "ergebnis": "result", "ergebnisse": "results",
    "ergebnisse_fuer": "results_for",
    "zustand": "state", "zustaende": "states",
    "befehl": "command", "befehle": "commands",
    "bericht": "report", "berichte": "reports",
    "schluessel": "key", "schluesseln": "keys",
    "zaehler": "counter",
    "kopf": "head", "kopf_hier": "head_here", "kopf_nachher": "head_after",
    "kopf_eins": "head_one",
    "teile": "parts", "teile_u2b": "parts_u2b",
    "eltern": "parent", "eltern_git": "parent_git",
    "breite": "width",
    "luecke": "gap", "luecken": "gaps",
    "grund": "reason", "gruende": "reasons", "grund_text": "reason_text",
    "ziel": "target", "ziele": "targets", "ziel_id": "target_id",
    "antwort": "answer", "antworten": "answers",
    "frage": "question", "fragen": "questions",
    "fehler": "error", "fehlerklasse": "error_class",
    "eintrag": "entry", "eintraege": "entries",
    "kandidat": "candidate", "kandidaten": "candidates",
    "knoten": "nodes", "knoten_gesamt": "nodes_total",
    "herkunft": "origin",
    "probleme": "problems",
    "betreff": "subject",
    "abschnitt": "section",
    "prueferin": "checker", "pruefer": "checker", "pruefung": "check",
    "schreiber": "writer", "laeufer": "runner",
    "laeufe": "runs",
    "unterschied": "differing",
    "zahlen": "numbers", "konflikte": "conflicts",
    # verbs
    "pruefe": "check", "pruefen": "check_all", "schreibe": "write",
    "verifiziere": "verify", "waehle": "choose", "anwenden": "apply_now",
    "nachziehen": "catch_up",
    # adjectives / adverbs / particles
    "erwartet": "expected", "geaendert": "changed", "gefunden": "found",
    "fehlt": "missing", "anerkannt": "acknowledged", "verboten": "forbidden",
    "damals": "then", "jetzt": "now", "vorher": "before", "nachher": "after",
    "hier": "here", "aus": "out", "ohne": "without",
    "neue": "new_", "neuer": "newer", "neuer_text": "new_text",
    "alle": "all_", "alle_formen": "all_shapes",
    "ist": "is_", "ist_shell": "is_shell",
    "offen": "open_", "offene": "open_",
    "nein": "refuse", "nur": "only",
    "gesamt": "total",
    "nicht_zugeordnet": "unattributed",
    "zeile": "line", "zeilen": "lines",
    "zelle": "cell", "zellen": "cells",
    "basis": "baseline", "arenen": "arenas",
    "leer": "empty", "Leer": "Empty", "gruen": "green", "Gruen": "Green",
    "englisch": "english", "Zustand": "State",
    "funktionen": "functions", "benutzt": "used", "wirklich": "genuinely",
    "einmal": "once", "zweimal": "twice", "kollision": "clash",
    "vorhanden": "present",
    "nach_kategorie": "by_category", "nach_sha": "by_sha",
    "fremd_nach_kategorie": "foreign_by_category",
}

#: Whole identifiers, translated as a unit. A compound is not the sum of its
#: parts -- `neu_bezeugen` is "witness again", not "new witness" -- so these
#: are written out rather than composed, and each one is a decision somebody
#: can disagree with by reading this line. Unique across the repository, which
#: is why they live here and not in OVERRIDES.
COMPOUNDS: dict[str, str] = {
    # src/hoh
    "VERBOTEN": "FORBIDDEN",
    "GIT_ZUSTAND": "GIT_STATE",
    "_eintrag": "_entry",
    "neu_bezeugen": "witness_again",
    "nach_annahme": "after_acceptance",
    "unklar": "unclear",
    "Unklar": "Unclear",
    "_revalidation_offen": "_revalidation_open",
    "_wieder_offen": "_reopened",
    "vorher_verletzt": "previously_violated",
    "spec_pfad": "spec_path",
    "vorher_id": "previous_id",
    "neu_state": "fresh_state",
    "vorheriger_zustand": "previous_state",
    "NICHT_ANGEWANDT": "NOT_APPLIED",
    "beweis_zeilen": "proof_lines",
    "isolation_grund": "isolation_reason",
    "weich_jetzt": "soft_now",
    "fremd": "foreign",
    "lauf": "run",
    "_lauf": "_run",
    "neu": "fresh",
    "NEU": "FRESH",
    "wo": "where",
    "mit": "with_",
    "nie": "never",
    "kontrolle": "control",
    # tools
    "FREMDE_HISTORIEN": "FOREIGN_HISTORIES",
    "_anker_vorhanden": "_anchor_exists",
    "_anker": "_anchor",
    "anker": "anchor",
    "_lauf_nachweis": "_run_evidence",
    "_nur_das_ledger": "_only_the_ledger",
    "knoten_entwickelt": "nodes_developed",
    "knoten_produkt": "nodes_by_product",
    "ERGEBNISSE": "RESULTS",
    "_ist_vertraut": "_is_trusted",
    "_worktree_aus": "_worktree_from",
    "trust_und_pruefen": "trust_and_check",
    "zellen_gesamt": "cells_total",
    "ziel_dir": "target_dir",
    "ERWARTET": "EXPECTED",
    "_fremder_prozess": "_foreign_process",
    "_kontrolle_je_lauf": "_control_per_run",
    "ohne_dispatchgrenze": "without_dispatch_ceiling",
    "ohne_feld": "without_field",
    "ohne_pruefung": "without_check",
    "_PFAD_ZEICHEN": "_PATH_CHARS",
    "_ist_ausgeschlossen": "_is_excluded",
    "BUDGET_HERKUNFT": "BUDGET_ORIGIN",
    "_je_lauf": "_per_run",
    "NUR_GIT": "GIT_ONLY",
    "arenen_ohne_bindung": "arenas_without_binding",
    "fremde_quittungen": "foreign_receipts",
    "im_baum": "in_tree",
    "kandidat_arenen": "candidate_arenas",
    "ZIEL": "TARGET",
    "je_pfad": "per_path",
    "U2B_ANERKANNT": "U2B_ACKNOWLEDGED",
    "BERICHTE": "REPORTS",
    "include_pfade": "include_paths",
    "kopf_vorher": "head_before",
    "lade_zustand": "load_state",
    "_kandidaten_einmal": "_candidates_once",
    "f_zustand": "f_state",
    "_datei": "_file",
    "commit_fehlt": "commit_missing",
    "digeste_im_commit": "digests_in_commit",
    "im_commit": "in_commit",
    "FEHLT": "MISSING",
    "NEIN": "NO",
    "_ledger_nachziehen": "_catch_up_ledger",
    "mit_luecken": "with_gaps",
    "verteilung_abschnitt": "distribution_section",
    "PROTOKOLL_PFAD": "LOG_PATH",
    "_BUDGET_ZEILE": "_BUDGET_ROW",
    "_REP_ZEILE": "_REP_ROW",
    "LEER_IST_EIN_WERT": "EMPTY_IS_A_VALUE",
    "NULL_IST_EIN_WERT": "NULL_IS_A_VALUE",
    "_zustand": "_state",
    "aggregation_kontrolle": "aggregation_control",
    "ziele": "targets_",
    "zeile_tests": "row_tests",
    "zeile_lint": "row_lint",
    "zeile_claims": "row_claims",
    "zeile_union": "row_union",
    "zeile_meta": "row_meta",
    "zeile_attribution": "row_attribution",
    "zeile_succession": "row_succession",
    "zeile_export_sync": "row_export_sync",
    "zeile_export": "row_export",
    "zeile_install": "row_install",
    "zeile_confinement": "row_confinement",
    "zeile_budget": "row_budget",
    "zeile_closure": "row_closure",
    "zeile_telemetrie": "row_telemetry",
    "zeile_benchmark_v2": "row_benchmark_v2",
    "zeile_benchmark_v3": "row_benchmark_v3",
    "zeile_prereg": "row_prereg",
    "zeile_ci": "row_ci",
    "zeile_audit": "row_audit",
    "zeile_routing": "row_routing",
    "zeile_identifiers": "row_identifiers",
    "zeile_evidenzindex": "row_evidence_index",
    "WURZEL": "ROOT", "PFAD": "PATH", "NACHWEIS": "EVIDENCE",
    "FREMD_SHA": "FOREIGN_SHA",
    "andere_arenen": "other_arenas", "arenen_veraendert": "arenas_changed",
    "arena_mit_datei": "arena_with_file",
    "basis_exit": "baseline_exit", "is_basis": "is_baseline",
    "_zellen_gruppiert": "_cells_grouped", "_gezaehlte_zelle": "_counted_cell",
    "_zelle": "_cell", "je_zelle": "per_cell",
    "aus_reportzeilen": "from_report_rows",
    "rc_erwartet": "rc_expected", "rc_alle": "rc_all",
    "ohne_head": "without_head", "ohne_kontrolle": "without_control",
    "ohne_log": "without_log", "_ohne_witness": "_without_witness",
    "t_mit": "t_with", "t_ohne": "t_without",
    "_fremde_historie": "_foreign_history", "_lauf_zustand": "_run_state",
    "intern_nur": "internal_only", "_mit_manifest": "_with_manifest",
    "_repo_mit": "_repo_with", "_mit_gates": "_with_gates",
    "_projekt_mit_knoten": "_project_with_nodes",
    "_abschnitt_mit": "_section_with", "git_mit_rennen": "git_with_race",
    "_export_im_fixture": "_export_in_fixture",
    "_kandidat": "_candidate", "_schreibe": "_write",
    "_json_bericht": "_json_report", "starte_lauf": "start_run",
    "lauf_dir": "run_dir", "_offen": "_open", "keine_gates": "no_gates",
    "_gruen": "_green", "hier_verankert": "anchored_here",

}


#: Test names, translated whole. 120 of them: the suite's names are its
#: documentation, and a German sentence in a published test file is the
#: same problem as a German identifier with more words in it. Written out
#: because a sentence is not a compound (O188).
TEST_NAMES: dict[str, str] = {
    "test_abgebrochener_lauf_ist_unentschieden":
        "test_a_cancelled_run_is_undetermined",
    "test_ablehnung_ist_eingabe_der_naechsten_iteration":
        "test_a_rejection_is_input_to_the_next_iteration",
    "test_absturz_nach_dem_merge_wird_nicht_doppelt_gemergt":
        "test_a_crash_after_the_merge_is_not_merged_twice",
    "test_absturz_vor_dem_statuswechsel_verliert_nichts":
        "test_a_crash_before_the_status_change_loses_nothing",
    "test_aktionsklasse_kommt_aus_dem_zustand_nicht_aus_der_spec":
        "test_the_action_class_comes_from_the_state_not_from_the_spec",
    "test_akzeptiert_aber_nicht_gelandet_wird_nicht_geraten":
        "test_accepted_but_not_landed_is_not_guessed",
    "test_andere_blockade_wird_nicht_als_ablehnung_geraten":
        "test_another_block_is_not_guessed_to_be_a_rejection",
    "test_aufgegebener_knoten_blockiert_seine_nachfolger_nicht":
        "test_an_abandoned_node_does_not_block_its_successors",
    "test_basislinie_wird_vor_dem_dispatch_persistiert":
        "test_the_baseline_is_persisted_before_the_dispatch",
    "test_bereits_angenommener_kandidat_wird_nicht_neu_dispatcht":
        "test_an_already_accepted_candidate_is_not_dispatched_again",
    "test_beschaedigter_zustand_blockiert_statt_zu_ueberschreiben":
        "test_a_damaged_state_blocks_instead_of_overwriting",
    "test_beschaedigter_zustand_haelt_an_statt_zu_ueberschreiben":
        "test_a_damaged_state_halts_instead_of_overwriting",
    "test_checkpointed_ohne_kandidat_ist_unentschieden":
        "test_checkpointed_without_a_candidate_is_undetermined",
    "test_cli_list_zeigt_jedes_projekt_mit_verdikt":
        "test_cli_list_shows_every_project_with_its_verdict",
    "test_cli_meldet_unlesbaren_zustand_mit_eigenem_exitcode":
        "test_the_cli_reports_an_unreadable_state_with_its_own_exit_code",
    "test_cli_record_action_misst_den_head_selbst":
        "test_cli_record_action_measures_the_head_itself",
    "test_cli_resume_ist_maschinenlesbar":
        "test_cli_resume_is_machine_readable",
    "test_cli_unblock_hat_einen_eigenen_exitcode_fuer_verweigerung":
        "test_cli_unblock_has_its_own_exit_code_for_a_refusal",
    "test_das_ergebnis_sagt_wie_es_lief":
        "test_the_result_says_how_it_ran",
    "test_der_elternbaum_ist_drinnen_nicht_sichtbar":
        "test_the_parent_tree_is_not_visible_inside",
    "test_der_guard_faengt_diese_schon_vor_dem_sandkasten":
        "test_the_guard_catches_these_before_the_sandbox",
    "test_der_halt_traegt_die_konfliktdetails":
        "test_the_halt_carries_the_conflict_details",
    "test_der_kandidat_ist_drinnen_nur_lesbar":
        "test_the_candidate_is_read_only_inside",
    "test_der_launcher_haelt_jede_antwort_fest":
        "test_the_launcher_records_every_answer",
    "test_der_launcher_hat_ohne_konfiguration_keine_autoritaet":
        "test_the_launcher_has_no_authority_without_configuration",
    "test_der_pfad_wird_aufgeloest_bevor_er_verglichen_wird":
        "test_the_path_is_resolved_before_it_is_compared",
    "test_derselbe_kandidat_wie_vorher_ist_KEINE_annahme":
        "test_the_same_candidate_as_before_is_NOT_an_acceptance",
    "test_die_antwort_sagt_wofuer_sie_galt":
        "test_the_answer_says_what_it_was_for",
    "test_die_grenze_gilt_auch_fuer_kindprozesse":
        "test_the_ceiling_applies_to_child_processes_too",
    "test_die_umgebung_wird_nicht_geerbt":
        "test_the_environment_is_not_inherited",
    "test_digest_ist_reihenfolgeunabhaengig":
        "test_the_digest_is_order_independent",
    "test_divergenz_unbekannter_zustand_wird_verhindert_statt_erkannt":
        "test_divergence_an_unknown_state_is_prevented_not_detected",
    "test_doppelter_orchestratorstart_wird_abgewiesen":
        "test_a_second_orchestrator_start_is_refused",
    "test_echter_fixpunkt_nennt_seinen_gegenstand":
        "test_a_real_fixpoint_names_its_subject",
    "test_echter_konflikt_wird_als_konflikt_erkannt":
        "test_a_real_conflict_is_recognised_as_a_conflict",
    "test_ein_begonnener_lauf_ist_nicht_ungestartet":
        "test_a_started_run_is_not_unstarted",
    "test_ein_echter_acceptance_check_laeuft_im_sandkasten":
        "test_a_real_acceptance_check_runs_in_the_sandbox",
    "test_ein_erschoepftes_budget_haelt_das_projekt_mit_eigener_klasse_an":
        "test_an_exhausted_budget_halts_the_project_under_its_own_class",
    "test_ein_fehlendes_hilfsskript_gibt_nicht_frei":
        "test_a_missing_helper_script_does_not_approve",
    "test_ein_neuer_angenommener_kandidat_ist_eine_annahme":
        "test_a_newly_accepted_candidate_is_an_acceptance",
    "test_ein_nie_begonnener_lauf_ist_nicht_unklar":
        "test_a_run_that_never_started_is_not_unclear",
    "test_ein_provider_gibt_nur_seinen_eigenen_worktree_frei":
        "test_a_provider_approves_only_its_own_worktree",
    "test_ein_scheiterndes_hilfsskript_gibt_nicht_frei":
        "test_a_failing_helper_script_does_not_approve",
    "test_ein_verzeichnis_als_scope_deckt_auch_kuenftige_worktrees":
        "test_a_directory_as_scope_covers_future_worktrees_too",
    "test_ein_zu_weiter_scope_wird_sofort_abgelehnt":
        "test_a_scope_that_is_too_broad_is_refused_at_once",
    "test_eine_aenderung_die_nichts_bewegt_ist_auch_eine":
        "test_a_change_that_moves_nothing_is_still_a_change",
    "test_entscheidung_ist_an_ihren_inhalt_gebunden":
        "test_a_decision_is_bound_to_its_content",
    "test_entscheidungen_ueberleben_den_neustart":
        "test_decisions_survive_the_restart",
    "test_erschoepftes_budget_mitten_im_lauf_ist_kein_fehlschlag":
        "test_an_exhausted_budget_mid_run_is_not_a_failure",
    "test_externe_aenderung_an_einem_unbekannten_knoten_wird_verweigert":
        "test_an_external_change_to_an_unknown_node_is_refused",
    "test_externe_aenderung_wird_mit_beiden_koepfen_festgehalten":
        "test_an_external_change_is_recorded_with_both_heads",
    "test_externe_aenderungen_stehen_nicht_unter_den_entscheidungen":
        "test_external_changes_do_not_sit_among_the_decisions",
    "test_externe_aenderungen_ueberleben_den_neustart":
        "test_external_changes_survive_the_restart",
    "test_externe_aktion_bleibt_als_solche_erkennbar":
        "test_an_external_action_stays_recognisable_as_one",
    "test_freigabebedarf_haelt_als_captain_gate_an":
        "test_an_approval_need_halts_as_a_captain_gate",
    "test_gate_ergebnis_traegt_seinen_gegenstand":
        "test_a_gate_result_carries_its_subject",
    "test_home_und_tmpdir_zeigen_in_den_scratch":
        "test_home_and_tmpdir_point_into_the_scratch",
    "test_jeder_halt_traegt_eine_klasse":
        "test_every_halt_carries_a_class",
    "test_kill_nach_merge_vor_globaler_closure_verlangt_reparatur":
        "test_a_kill_after_the_merge_and_before_global_closure_demands_a_repair",
    "test_kill_waehrend_der_reparaturknoten_erzeugung":
        "test_a_kill_while_the_repair_node_is_being_created",
    "test_kill_waehrend_eines_laufs_wird_ausgewertet_nicht_geraten":
        "test_a_kill_during_a_run_is_evaluated_not_guessed",
    "test_kill_waehrend_planung_laesst_knoten_ready":
        "test_a_kill_during_planning_leaves_the_node_ready",
    "test_laufender_knoten_mit_ablehnung_geht_zurueck_in_die_schleife":
        "test_a_running_node_with_a_rejection_goes_back_into_the_loop",
    "test_laufender_knoten_ohne_erkennbares_ergebnis_haelt_an":
        "test_a_running_node_with_no_recognisable_result_halts",
    "test_laufender_knoten_wird_gelesen_nicht_neu_gestartet":
        "test_a_running_node_is_read_not_restarted",
    "test_leere_gate_menge_ist_nicht_gruen":
        "test_an_empty_gate_set_is_not_green",
    "test_limit_4_der_radius_schrumpft_aber_der_guard_bleibt_eine_denylist":
        "test_limit_4_the_radius_shrinks_but_the_guard_stays_a_denylist",
    "test_limit_5_bleibt_offen_weil_es_kein_isolationsproblem_ist":
        "test_limit_5_stays_open_because_it_is_not_an_isolation_problem",
    "test_limit_6_git_klettert_draussen_raus_und_drinnen_nicht":
        "test_limit_6_git_climbs_out_outside_and_not_inside",
    "test_limit_6_im_echten_runner_pfad":
        "test_limit_6_on_the_real_runner_path",
    "test_lock_haelt_ueber_prozessgrenzen":
        "test_the_lock_holds_across_process_boundaries",
    "test_negative_kontrollen_im_runner_pfad":
        "test_negative_controls_on_the_runner_path",
    "test_netzwerk_ist_standardmaessig_aus":
        "test_the_network_is_off_by_default",
    "test_neuer_orchestrator_ohne_jeden_kontext_entscheidet_gleich":
        "test_a_fresh_orchestrator_with_no_context_decides_the_same",
    "test_nicht_ausgefuehrte_gates_sind_kein_fixpunkt":
        "test_gates_that_did_not_run_are_not_a_fixpoint",
    "test_nie_begonnener_knoten_wird_wieder_zu_arbeit":
        "test_a_node_that_never_started_becomes_work_again",
    "test_nosandbox_laeuft_nur_wenn_ausdruecklich_verlangt":
        "test_nosandbox_runs_only_when_explicitly_asked_for",
    "test_not_run_zaehlt_nie_als_gruen":
        "test_not_run_never_counts_as_green",
    "test_nur_das_juengste_ergebnis_je_gate_zaehlt":
        "test_only_the_most_recent_result_per_gate_counts",
    "test_offener_reparaturknoten_verhindert_closure":
        "test_an_open_repair_node_prevents_closure",
    "test_ohne_backend_faellt_strict_geschlossen_aus":
        "test_without_a_backend_strict_fails_closed",
    "test_ohne_grenzen_wird_kein_hook_installiert":
        "test_without_ceilings_no_hook_is_installed",
    "test_ohne_isolation_bleibt_alles_wie_bisher":
        "test_without_isolation_everything_stays_as_before",
    "test_ohne_provider_wird_nichts_freigegeben":
        "test_without_a_provider_nothing_is_approved",
    "test_paritaet_ablehnung_ist_keine_sackgasse":
        "test_parity_a_rejection_is_not_a_dead_end",
    "test_paritaet_externe_aktion_startet_nachweislich_nichts":
        "test_parity_an_external_action_demonstrably_starts_nothing",
    "test_paritaet_mit_dem_prototyp":
        "test_parity_with_the_prototype",
    "test_paritaet_unbekanntes_verdikt_haelt_beide_an":
        "test_parity_an_unknown_verdict_halts_both",
    "test_projekte_werden_aufgelistet":
        "test_projects_are_listed",
    "test_provider_ausfall_ist_kein_verdikt":
        "test_a_provider_outage_is_not_a_verdict",
    "test_provider_blockade_ist_keine_ablehnung":
        "test_a_provider_block_is_not_a_rejection",
    "test_ready_for_delivery_zaehlt_wie_checkpointed":
        "test_ready_for_delivery_counts_as_checkpointed",
    "test_reparatur_wird_als_entscheidung_festgehalten":
        "test_a_repair_is_recorded_as_a_decision",
    "test_require_erscheint_in_der_ausgabe":
        "test_require_appears_in_the_output",
    "test_require_macht_ein_uebersprungenes_invariant_rot":
        "test_require_turns_a_skipped_invariant_red",
    "test_require_wird_streng_gelesen":
        "test_require_is_read_strictly",
    "test_schreiben_auf_das_ein_anderer_liest_ist_abhaengig":
        "test_writing_what_another_reads_is_dependent",
    "test_schreiben_loescht_nie_den_vorgaenger":
        "test_a_write_never_deletes_its_predecessor",
    "test_scratch_ist_schreibbar_und_liegt_nicht_im_kandidaten":
        "test_scratch_is_writable_and_lies_outside_the_candidate",
    "test_speichergrenze_wird_wirklich_erzwungen":
        "test_the_memory_ceiling_is_actually_enforced",
    "test_stale_writer_haelt_die_schleife_an":
        "test_a_stale_writer_halts_the_loop",
    "test_stale_writer_wird_abgewiesen":
        "test_a_stale_writer_is_refused",
    "test_start_ist_idempotent":
        "test_start_is_idempotent",
    "test_strict_ohne_backend_faellt_geschlossen_aus_statt_ungesandboxt_zu_laufen":
        "test_strict_without_a_backend_fails_closed_instead_of_running_unsandboxed",
    "test_terminaler_dag_ist_keine_geschlossene_freigabe":
        "test_a_terminal_dag_is_not_a_closed_release",
    "test_terminaler_dag_mit_roter_closure_erzeugt_reparatur":
        "test_a_terminal_dag_with_a_red_closure_creates_a_repair",
    "test_trockenlauf_meldet_NOT_RUN_und_mergt_nicht":
        "test_a_dry_run_reports_NOT_RUN_and_does_not_merge",
    "test_trust_dialog_ist_freigabebedarf_nicht_unklar":
        "test_a_trust_dialog_is_an_approval_need_not_an_unclear_state",
    "test_ueberlappende_schreibmengen_sind_abhaengig":
        "test_overlapping_write_sets_are_dependent",
    "test_unaufloesbare_abhaengigkeit_startet_nichts":
        "test_an_unresolvable_dependency_starts_nothing",
    "test_unbekannte_abhaengigkeit_blockiert_statt_zu_starten":
        "test_an_unknown_dependency_blocks_instead_of_starting",
    "test_unbekannter_mergefehler_haelt_weiterhin_ambiguous_an":
        "test_an_unknown_merge_error_still_halts_as_ambiguous",
    "test_unbekannter_mergefehler_wird_nicht_zum_konflikt_gemacht":
        "test_an_unknown_merge_error_is_not_turned_into_a_conflict",
    "test_unblock_kennt_unbekannte_knoten_nicht":
        "test_unblock_does_not_know_unknown_nodes",
    "test_unblock_verlangt_einen_grund_und_schreibt_ihn_auf":
        "test_unblock_demands_a_reason_and_writes_it_down",
    "test_unblock_verweigert_was_nicht_blockiert_ist":
        "test_unblock_refuses_what_is_not_blocked",
    "test_unfertiger_lauf_ohne_budgetgrenze_ist_eine_ablehnung":
        "test_an_unfinished_run_without_a_budget_ceiling_is_a_rejection",
    "test_verstellter_arbeitsbaum_ist_kein_konflikt":
        "test_a_disturbed_working_tree_is_not_a_conflict",
    "test_wirklich_unabhaengige_knoten_werden_nicht_serialisiert":
        "test_genuinely_independent_nodes_are_not_serialized",
    "test_zustand_ist_reines_json_und_wieder_einlesbar":
        "test_the_state_is_plain_json_and_can_be_read_back",
}


#: Batch translated 2026-09-21 after the stem list was widened (O191).
BULK: dict[str, str] = {
    "ALT": "OLD",
    "AUFGABEN": "TASKS",
    "BinaererBeweis": "BinaryProof",
    "DARF_UNBEKANNT_SEIN": "MAY_BE_UNKNOWN",
    "ECHT": "REAL",
    "FALSIFIKATOREN": "FALSIFIERS",
    "GIT_VERZEICHNISSE": "GIT_DIRECTORIES",
    "HIER": "HERE",
    "INTERNE_DOKUMENTE": "INTERNAL_DOCUMENTS",
    "KAMPAGNEN": "CAMPAIGNS",
    "KAMPAGNEN_DIR": "CAMPAIGNS_DIR",
    "Protokoll": "Log_",
    "ROLLEN_PRO_ITERATION": "ROLES_PER_ITERATION",
    "Schritt": "Step_",
    "UMGEBUNG": "ENVIRONMENT",
    "Zurueckgehalten": "Withheld",
    "_EIGENE_SCRATCHES": "_OWN_SCRATCHES",
    "_GEPARKT": "_PARKED",
    "_angriff": "_attack",
    "_baum": "_tree",
    "_beginne_zaehlung": "_begin_count",
    "_commit_feld": "_commit_field",
    "_endzustand": "_end_state",
    "_env_aufnahme": "_env_capture",
    "_falsifiziere_eingrenzung": "_falsify_confinement",
    "_falsifiziere_quittung": "_falsify_receipt",
    "_felder": "_fields",
    "_frisches_scratch": "_fresh_scratch",
    "_gelesen": "_read",
    "_geparkt": "_parked",
    "_gezaehlt_oder_behauptet": "_counted_or_claimed",
    "_gutes_artefakt": "_good_artifact",
    "_hart": "_hard",
    "_konfiguriert": "_configured",
    "_laden": "_load",
    "_letzte_aufrufe": "_last_calls",
    "_messen": "_measure",
    "_modus": "_mode",
    "_quittung": "_receipt",
    "_quittungen_geschrieben": "_receipts_written",
    "_satz": "_sentence",
    "_strict_quittungen": "_strict_receipts",
    "_telemetrie": "_telemetry",
    "_vergleich": "_comparison",
    "_zeuge_umfang": "_witness_scope",
    "alt": "old",
    "alt_capsule": "old_capsule",
    "alt_digest": "old_digest",
    "alt_dir": "old_dir",
    "alt_hoh": "old_hoh",
    "alte": "old_",
    "alte_quittungen": "old_receipts",
    "anforderung": "requirement",
    "angewandt": "applied_",
    "arbeit": "work_",
    "arbeiten": "work_items",
    "arbeitsbaum": "working_tree",
    "artefakt": "artifact_",
    "auf_platte": "on_disk",
    "aufgabe": "task_",
    "aufgaben": "tasks_",
    "aufrufe": "calls_",
    "ausgabe": "output_",
    "ausgaben": "outputs_",
    "ausgang": "outcome_",
    "aussen": "outside_",
    "baeume": "trees_",
    "baum": "tree_",
    "bekannt": "known",
    "benchmark_aufgaben": "benchmark_tasks",
    "beratend": "advisory",
    "bereit": "ready_",
    "bestanden": "passed_",
    "beste": "best",
    "bewegt": "moved_",
    "beweis": "proof_",
    "beweist": "proves",
    "binaererbeweis": "binary_proof",
    "bindend": "binding_",
    "bindungen": "bindings",
    "braucht_bwrap": "needs_bwrap",
    "braucht_evidenz": "needs_evidence",
    "braucht_geparkten_vorgaenger": "needs_parked_predecessor",
    "daten": "data_",
    "dauer": "duration_",
    "dispatch_zaehlung": "dispatch_count",
    "draussen": "outside",
    "drift_abgerechnet": "drift_accounted",
    "echt": "real",
    "echt_budget": "real_budget",
    "echt_gate": "real_gate",
    "echt_git": "real_git",
    "echt_hoh": "real_hoh",
    "echt_staging": "real_staging",
    "echt_state": "real_state",
    "echte": "real_",
    "echte_identitaet": "real_identity",
    "echter": "real_one",
    "eigen": "own",
    "eigen_ns": "own_ns",
    "eigene": "own_",
    "endzustand": "end_state",
    "erlaubt": "allowed_",
    "erst": "first_",
    "erste": "first",
    "erste_arena": "first_arena",
    "erste_kopie": "first_copy",
    "erster": "first_one",
    "erster_zellstart": "first_cell_start",
    "erstes": "first_item",
    "falsch": "wrong",
    "falsche": "wrong_",
    "falsifikator": "falsifier",
    "falsifikatoren": "falsifiers",
    "fehlen": "missing_ones",
    "fehlend": "missing_",
    "feld": "field_",
    "felder": "fields_",
    "felder_ok": "fields_ok",
    "folge": "sequence_",
    "freigabe": "release_",
    "frisch": "fresh_",
    "gate_aufrufe": "gate_calls",
    "gate_folge": "gate_sequence",
    "geehrt": "honoured",
    "gelesen": "read_",
    "gemeinsam": "shared",
    "gemessen": "measured_",
    "geparkt": "parked",
    "geparkten": "parked_",
    "gesehen": "seen_",
    "gespeichert": "stored",
    "gezaehlt": "counted",
    "gezaehlt_echt": "counted_real",
    "gezaehlte_dispatches": "counted_dispatches",
    "grenze": "limit_",
    "grenzen": "limits_",
    "gruppe": "group_",
    "gruppen": "groups_",
    "gut": "good",
    "gutes": "good_",
    "halluziniertes_feld": "hallucinated_field",
    "hart": "hard",
    "historie_vollstaendig": "history_complete",
    "home_teil": "home_part",
    "home_wert": "home_value",
    "inhalt": "content_",
    "intern": "internal_",
    "intern_nur": "internal_only",
    "interne": "internal_ones",
    "kampagne": "campaign_",
    "kampagnen": "campaigns_",
    "kampagnen_urteil": "campaign_verdict",
    "kampagnenkopf": "campaign_head",
    "kette": "chain",
    "klasse": "kind",
    "klassen": "classes_",
    "konfiguriert": "configured_",
    "kopie": "copy_",
    "kopien": "copies_",
    "kriterien": "criteria_",
    "laden": "load_",
    "lese_ende": "read_end",
    "max_runden": "max_rounds",
    "meldung": "message_",
    "messen": "measure_",
    "modul": "module_",
    "modus": "mode_",
    "muster": "pattern_",
    "nach": "after",
    "nach_hook": "after_hook",
    "nach_name": "after_name",
    "nach_rolle": "by_role",
    "nach_typ": "by_type",
    "namen": "names_",
    "netz": "network_",
    "noetig": "needed",
    "politik": "policy_",
    "produkt_klassen": "product_classes",
    "produkt_verdikte": "product_verdicts",
    "proto_ende": "proto_end",
    "protokoll": "log_",
    "protokolle": "logs_",
    "puffer": "buffer_",
    "quittung": "receipt_",
    "quittungen": "receipts_",
    "raus": "out_list",
    "roh": "raw_",
    "rolle": "role_",
    "rollen": "roles_",
    "rot": "red",
    "runde": "round_",
    "runden": "rounds_",
    "saetze": "sentences",
    "satz": "sentence",
    "schlecht": "bad",
    "schluss": "conclusion_",
    "schreib_ende": "write_end",
    "schritt": "step_",
    "schritte": "steps_",
    "skript": "script_",
    "stempel": "stamp",
    "stempel_digeste": "stamp_digests",
    "tabelle": "table_",
    "teil": "part_",
    "telemetrie": "telemetry_",
    "tmp_teil": "tmp_part",
    "tmp_wert": "tmp_value",
    "tun": "do_",
    "uebrig": "remaining_",
    "umgebung": "environment_",
    "unbekannt": "unknown_",
    "unter": "below",
    "verbleibendes_budget": "remaining_budget",
    "verbrauchtes_budget": "spent_budget",
    "verdikt": "verdict",
    "verdikte": "verdicts",
    "vergleich": "comparison",
    "vergleiche": "comparisons",
    "verlangt": "demands",
    "verworfen": "discarded",
    "verzeichnis": "directory",
    "verzeichnisse": "directories",
    "voll": "full_",
    "vollstaendig": "complete_",
    "von": "from_",
    "wann": "when_",
    "warum": "why_text",
    "weich": "soft",
    "welt": "world",
    "wert": "value_",
    "werte": "values_",
    "zaehlung": "count_",
    "zeuge": "witness_",
    "zurueck": "back",
    "zweig": "branch_",
    "zweit": "second_",
    "zweite": "second",
    "zweite_aggregation": "second_aggregation",
    "zweite_text": "second_text",
    "zweiter": "second_one",
    "ANDERER_HEAD": "OTHER_HEAD",
    "ARTEFACTUAL_SIGNATUREN": "ARTEFACTUAL_SIGNATURES",
    "AUDIT_PRUEFUNGEN": "AUDIT_CHECKS",
    "Abbruch": "Abort",
    "Ausfall": "Outage",
    "BEDINGT": "CONDITIONAL",
    "Dazwischen": "InBetween",
    "ENTSPRECHUNG": "EQUIVALENT",
    "EREIGNISSE": "EVENTS",
    "ERKLAERTES_BUDGET": "DECLARED_BUDGET",
    "Erschoepft": "Exhausted",
    "GEFROREN": "FROZEN",
    "KATEGORIEN": "CATEGORIES",
    "KONTROLLEN": "CONTROLS",
    "Kaputt": "Broken",
    "Keiner": "NoneOfThem",
    "Kollidiert": "Collides",
    "Kurz": "Short",
    "Merkend": "Remembering",
    "PROTOTYP": "PROTOTYPE",
    "ROUTING_ENTSCHIEDEN": "ROUTING_DECIDED",
    "Raetselhaft": "Puzzling",
    "Stirbt": "Dies",
    "Stumm": "Silent",
    "TYP": "KIND",
    "Unbrauchbar": "Unusable",
    "VCS_METADATEN": "VCS_METADATA",
    "Versender": "Sender",
    "Wackelig": "Shaky",
    "Wartet": "Waits",
    "_AUSGESCHLOSSEN": "_EXCLUDED",
    "_BUDGET_PROSA": "_BUDGET_PROSE",
    "_als_objekt": "_as_object",
    "_anfang": "_beginning",
    "_anhaengen": "_append",
    "_antwortdatei": "_answer_file",
    "_blockiert": "_blocked",
    "_budget_erschoepft": "_budget_exhausted",
    "_budget_oder_absage": "_budget_or_refusal",
    "_erforderliche_wiederholungen": "_required_repetitions",
    "_fortsetzung": "_continuation",
    "_geschlossen": "_closed",
    "_groesse": "_size",
    "_halte_lock": "_hold_lock",
    "_identitaet": "_identity",
    "_konformitaet": "_conformance",
    "_oeffnend": "_opening",
    "_prototyp_faehrt": "_prototype_runs",
    "_rlimit_paare": "_rlimit_pairs",
    "_sauber": "_clean",
    "_schmutzig": "_dirty",
    "_schreiben": "_write_file",
    "_transkript": "_transcript",
    "_verfolgt": "_tracked",
    "abdeckung": "coverage_",
    "abgelehnt": "rejected_",
    "abgerechnet": "accounted",
    "abgesagt": "refused_",
    "absage": "refusal_",
    "abweichend": "differing_",
    "aenderung": "change_",
    "aktuell": "current_",
    "anderer": "other_",
    "anderswo": "elsewhere",
    "angefordert": "requested_",
    "angenommen": "accepted_",
    "anhaengen": "append_",
    "aufgeloest": "resolved_",
    "aufgezeichnet": "recorded_",
    "ausfuehrlich": "verbose",
    "ausgeschlossen": "excluded_",
    "ausser": "except_",
    "ausserhalb": "outside_of",
    "beantwortet": "answered_",
    "bedingung": "condition_",
    "behalten": "kept",
    "behauptet": "claimed_",
    "beob": "observed_calls",
    "beobachtet": "observed_",
    "bereich": "region",
    "beruehrt": "touched",
    "beschattet": "shadowed",
    "beschreibung": "description_",
    "bestaetigt": "confirmed_",
    "betroffen": "affected",
    "bewacht": "guarded",
    "blockiert": "blocked_",
    "commit_abweichung": "commit_deviation",
    "confinement_zusammenfassung": "confinement_summary",
    "datensatz": "record_",
    "dazu": "alongside",
    "digeste_gleich": "digests_equal",
    "doppelt": "twice_",
    "dreck": "debris",
    "drin": "inside_it",
    "drinnen": "inside",
    "dritte": "third",
    "drueber": "above_it",
    "eing": "confined",
    "eingeschlossen": "included_",
    "eingrenzung": "confinement_",
    "entdeckt": "discovered",
    "entfernt": "removed_",
    "entscheidbar": "decidable",
    "entwertet": "invalidated",
    "entwickeln": "develop",
    "entwickler": "developer_",
    "entwickler_diff": "developer_diff",
    "erg": "outcome_value",
    "erkannt": "recognised",
    "erledigt": "done_",
    "erneuert": "renewed",
    "ernst": "serious",
    "erreichbar": "reachable_",
    "erschoepft": "exhausted_",
    "erzwingend": "enforcing",
    "faelle": "cases_",
    "faelschen": "forge_",
    "faelschung": "forgery",
    "fern": "remote_",
    "fertig": "finished_",
    "fest": "fixed_",
    "festgeschrieben": "committed_",
    "flach": "flat_",
    "fluechtig": "transient",
    "fortschritt": "progress_",
    "frueher": "earlier_",
    "frueheste": "earliest",
    "gebaut": "built",
    "gebunden": "bound_",
    "gedriftet": "drifted_",
    "geheim": "secret_",
    "gehoert_uns": "belongs_to_us",
    "gekuerzt": "truncated",
    "gelaufen": "ran",
    "geliefert": "delivered_",
    "gemischt": "mixed_",
    "genannt": "named_",
    "genommen": "taken",
    "gepflanzt": "planted",
    "geplant": "planned_",
    "geplante_checks": "planned_checks",
    "gescheitert": "failed_",
    "geschmuggelt": "smuggled",
    "geschrieben": "written",
    "geschrieben_haette": "would_have_written",
    "geschuetzt": "protected",
    "geschwister": "siblings",
    "gesetzt": "set_",
    "gestaged": "staged_",
    "gestartet": "started_",
    "gewuenscht": "wanted",
    "gleich": "equal_",
    "groesse": "size_",
    "haupt": "main_",
    "hindernis": "obstacle",
    "hindernisse": "obstacles",
    "hintergrund": "background",
    "hintertuer": "back_door",
    "hinzugefuegt": "added_",
    "hoechste": "highest",
    "hoechstens": "at_most",
    "holen": "fetch_",
    "identitaeten": "identities",
    "innen": "inside_",
    "installieren": "install_",
    "iterationen": "iterations_",
    "kapsel": "capsule_",
    "kapsel_rel": "capsule_rel",
    "kaputt": "broken_",
    "kennung": "identifier_",
    "klettert": "climbs",
    "konform": "conformant_",
    "kuenftig": "future_",
    "kurz": "short_",
    "laeuft": "is_running",
    "lief": "ran_",
    "los": "loose",
    "melde": "report_",
    "menschen": "humans",
    "menschlich": "human_",
    "metrik": "metric_",
    "muss": "must_",
    "nachsatz": "postscript",
    "naechste": "next_",
    "naiv": "naive_",
    "nenne_hindernisse": "name_obstacles",
    "notiz": "note_",
    "notizen": "notes_",
    "oben": "above",
    "ordner": "folder",
    "ort": "place",
    "orte": "places",
    "paare": "pairs",
    "pflanzen": "plant_",
    "pflanzungen": "plantings",
    "plaene": "plans_",
    "planer": "planner_",
    "planerin": "planner_role",
    "planner_gueltig": "planner_valid",
    "planner_saetze": "planner_sentences",
    "praefix": "prefix_",
    "prototyp": "prototype_",
    "punkt": "point_",
    "raeder": "wheels",
    "rang": "rank",
    "receipt_aenderungen": "receipt_changes",
    "regel": "rule_",
    "regel_fuer": "rule_for",
    "rendern": "render_",
    "sammeln": "collect_",
    "sammler": "collector_",
    "sandkasten": "sandbox_",
    "sauber": "clean_",
    "schmutz": "dirt",
    "schmutzig": "dirty_",
    "schon_weg": "already_spent",
    "schreiben": "write_ops",
    "schreibfehler": "write_error",
    "schrott": "junk",
    "schwach": "weak",
    "sonst": "otherwise",
    "spaeter": "later",
    "spanne": "span",
    "staging_pruefen": "staging_check",
    "staging_schmutz": "staging_dirt",
    "stapel": "stack_",
    "stopp": "stop_",
    "strict_zusammenfassung": "strict_summary",
    "stueck": "piece",
    "stuecke": "pieces",
    "stumm": "silent_",
    "subjekt": "subject_",
    "suche": "search_",
    "summe": "total_",
    "t_drin": "t_inside",
    "target_no_frag": "target_no_frag",
    "test_failure_line_nimmt_stderr_zuerst": "test_failure_line_takes_stderr_first",
    "test_nosandbox_verweigert_strict": "test_nosandbox_refuses_strict",
    "typ": "kind_",
    "ueber": "over",
    "ueberschreiben": "overwrite_",
    "uebersprungen": "skipped_",
    "umfang": "scope_",
    "unabhaengig": "independent_",
    "unattended_zusammenfassung": "unattended_summary",
    "unbedingt": "unconditionally",
    "unerklaert": "unexplained",
    "unerreichbar": "unreachable_",
    "ungueltig": "invalid_",
    "unser": "ours",
    "unsere": "ours_",
    "unten": "further_down",
    "unveraendert": "unchanged_",
    "veraendert": "changed_state",
    "veraltet": "stale_",
    "verbucht": "booked",
    "verbunden": "connected",
    "verfolgt": "tracked_",
    "verlauf": "course_",
    "verletzt": "violated",
    "verletzungen": "violations_",
    "veroeffentlicht": "published_",
    "verschieden": "different_",
    "verschwunden": "vanished",
    "versender": "sender",
    "versionen": "versions_",
    "versuch": "attempt_",
    "versuche": "attempts_",
    "verteilung": "distribution_",
    "verweigert": "refuses",
    "viele": "many",
    "vorab": "in_advance",
    "vorbereiten": "prepare_",
    "vorbestand": "pre_existing",
    "vorgaenger": "predecessor_",
    "vorsichtig": "cautious",
    "wechselnd": "alternating",
    "weit": "wide",
    "weiter": "further",
    "werkzeug": "tool_",
    "werkzeuge": "tools_",
    "wiederholung": "repetition_",
    "wort": "word_",
    "zahl": "number_",
    "zeichen": "chars_",
    "zu_schreiben": "to_write",
    "zu_weit": "too_broad",
    "zuhause": "home_dir",
    "zusicherung": "assurance_",
    "HistorieUnvollstaendig": "HistoryIncomplete",
    "LuegtMitBeweis": "LiesWithProof",
    "LuegtUeberIsolation": "LiesAboutIsolation",
    "MitNetz": "WithNetwork",
    "ZaehlenderDispatcher": "CountingDispatcher",
    "_GeprueftesScope": "_CheckedScope",
    "_aggregatzeilen": "_aggregate_rows",
    "_kampagnenkopf": "_campaign_head",
    "_letzte_retries": "_last_retries",
    "bwrap_nutzbar": "bwrap_usable",
    "letzte": "last_",
    "schlüssel": "key_name",
    "treffer": "hits_",
}

#: Where one German word is two English ones. Keyed by repository-relative
#: path; merged over DEFAULT for that file only.
OVERRIDES: dict[str, dict[str, str]] = {
    "tools/attribution.py": {"zeile": "line", "zeilen": "lines"},
    "tools/succession.py": {"zeile": "row", "zeilen": "rows"},
    "src/hoh/sandbox.py": {"zeile": "line", "zeilen": "lines"},
    # O189. Each of these files already has the obvious English word for a
    # *different* thing, so the German name gets a distinct one rather than
    # being merged into it. The alternative -- permitting the collision after
    # inspecting the scopes -- makes the tool's safety depend on a judgement
    # nobody can re-check from the diff.
    "src/hoh/capability.py": {"pfad": "file_path"},
    "src/hoh/dispatchers.py": {"ziel": "destination", "vorher": "earlier"},
    "src/hoh/store.py": {"ziel": "destination"},
    "src/hoh/telemetry.py": {"zeile": "text_line", "zeilen": "text_lines"},
    # Same rule for the tools: where the English word is already taken for
    # something else in that file, the German name gets a distinct one.
    "tools/check_claims.py": {"pfad": "file_path", "eintraege": "records"},
    "tools/clean_install_check.py": {"quelle": "source_file"},
    "tools/meta_evidence.py": {"pfad": "file_path", "zustand": "condition"},
    "tools/readiness.py": {"zeile": "row", "zeilen": "row_list",
                           "Zeile": "Row", "pfad": "file_path",
                           "jetzt": "moment"},
    "tools/telemetry_audit.py": {"pfad": "file_path",
                                 "zeile": "text_line"},
    # A test reaches into the module it tests by name, so every override above
    # is mirrored onto the test file that imports it. Without this the sweep
    # renames a function in one file and its only caller in another keeps the
    # old spelling -- 53 tests failed on exactly that, all of them
    # `AttributeError: module has no attribute 'pruefe'`.
    "tests/test_benchmark_campaigns.py": {"pfad": "file_path",
                                          "zustand": "condition",
                                          "schluessel": "lookup_key",
                                          "grund": "why"},
    "tests/test_confinement_evidence.py": {"wurzel": "base",
                                           "pfad": "file_path",
                                           "aus": "output",
                                           "lauf": "one_run"},
    "tests/test_meta_evidence.py": {"pfad": "file_path",
                                    "zustand": "condition"},
    "tests/test_readiness.py": {"zeile": "row", "zeilen": "rows",
                                "_zeile": "_row",
                                "Zeile": "Row", "pfad": "file_path",
                                "jetzt": "moment"},
    "tests/test_export_gap_guard.py": {"zeile_ci": "row_ci", "zeile": "row",
                                       "zeilen": "row_list", "Zeile": "Row",
                                       "pfad": "file_path", "jetzt": "moment"},
    "tests/test_attribution.py": {"zeile": "line", "zeilen": "lines",
                                  "lauf": "one_run"},
    "tests/test_export_sync.py": {"zeile": "line", "zeilen": "lines"},
    "tests/test_sandbox.py": {"zeile": "line", "zeilen": "lines"},
    "tests/test_assurance.py": {"zustand": "condition", "probleme": "issues"},
    "tests/test_capability.py": {"pfad": "file_path"},
    "tests/test_runner.py": {"jetzt": "moment"},
    "tests/test_amendment_e2e.py": {"aus": "output"},
    "tests/test_approval.py": {"eltern": "parent_dir"},
    "tests/test_export_manifest.py": {"pfad": "file_path", "teile": "segments",
                                      "ziel": "destination"},
    "tests/test_isolation_record.py": {"befehl": "cmdline",
                                       "kandidat": "candidate_tree",
                                       "gekuerzt": "was_truncated"},
    "tests/test_launcher_verdicts.py": {"zeile": "line", "zeilen": "lines",
                                        "zustand": "verdict_state",
                                        "kandidat": "candidate_tree"},
    "tests/test_policy_wiring.py": {"zustand": "policy_state",
                                    "entwickeln": "do_development",
                                    "geschuetzt": "is_protected"},
    "tests/test_prereg_provenance.py": {"wurzeln": "bases"},
    "tests/test_project_resume.py": {"grund": "why", "knoten": "node_list",
                                     "aus": "output"},
    "tests/test_telemetry_audit.py": {"pfad": "file_path", "aus": "output"},
    "tests/test_union_gate.py": {"aus": "output"},
    # A name that belongs to something outside this repository is pinned by
    # mapping it to itself. `hoh-operator-tools/autopilot.py` is the prototype
    # this suite checks parity against; it is a foreign tree, read-only, and
    # its API is German. Renaming the override here left the abstract method
    # unimplemented and four parity cases raised NotImplementedError from a
    # file this sweep must not touch. The same applies to `anchored_here`,
    # which is a field name in CLAIMS.json: data, not code.
    "tests/test_orchestrator_parity.py": {
        # Every name the foreign oracle exposes, pinned to itself.
        # `tests/test_identifiers.py` checks this list against the
        # module when that module is present on the machine.
        "Ausfuehrer": "Ausfuehrer",
        "BEKANNT": "BEKANNT",
        "ERLEDIGT": "ERLEDIGT",
        "HALT_RUNDEN": "HALT_RUNDEN",
        "HALT_UNKLAR": "HALT_UNKLAR",
        "Protokoll": "Protokoll",
        "WERKZEUGE": "WERKZEUGE",
        "bereit": "bereit",
        "fahre": "fahre",
        "globale_gates": "globale_gates",
        "klassifiziere": "klassifiziere",
        "knoten": "knoten",
        "schreibe": "schreibe",
        "semantische_abhaengigkeit": "semantische_abhaengigkeit",
        "starte_lauf": "starte_lauf",
        "reparaturknoten": "reparaturknoten",
        "unbekannte_zustaende": "unbekannte_zustaende",
        "zaehle": "zaehle",
        # `max_runden` is a keyword argument of the foreign
        # Autopilot, so it is pinned like every other name that
        # belongs to that module.
        "max_runden": "max_runden", "verdikte": "verdict_list",
    },
    "tools/audit_refs.py": {"hier_verankert": "hier_verankert"},
    "src/hoh/assurance.py": {"zustand": "condition", "probleme": "issues",
                             "klasse": "class_name"},
    "src/hoh/cli.py": {"grund": "why", "unklar": "undecided", "neu": "created",
                       "verdikt": "verdict_text"},
    "src/hoh/controller.py": {"schluessel": "lookup_key", "grund": "why",
                              "antwort": "reply", "bekannt": "known_nodes"},
    "src/hoh/orchestrator.py": {"grund": "why", "klasse": "class_name"},
    "src/hoh/runner.py": {"jetzt": "moment", "hart": "hard_kill"},
    "tools/collect_strict_evidence.py": {"kandidat": "candidate_tree",
                                         "laeufer": "runner_path",
                                         "geehrt": "was_honoured"},
    "tests/test_identifiers.py": {"quelle": "sample"},
    "tests/test_store.py": {"geparkt": "parked_files"},
    "tests/test_claims_anchors.py": {"pfad": "file_path", "eintraege": "records",
                                     "wurzel": "base", "quelle": "source_file",
                                     "hier_verankert": "hier_verankert"},
    # `lauf` is a local in files that already bind `run` -- usually the
    # function the local is passed to.
    "tools/benchmark.py": {"pfad": "file_path", "zustand": "condition",
                           "schluessel": "lookup_key", "grund": "why",
                           "lauf": "one_run"},
    "tools/budget_evidence.py": {"lauf": "one_run"},
    "tools/closure_e2e.py": {"knoten": "node_list", "lauf": "one_run"},
    "tools/confinement_evidence.py": {"wurzel": "base", "pfad": "file_path",
                                      "aus": "output", "lauf": "one_run"},
    "tools/exact_head_ci.py": {"lauf": "one_run"},
    "src/hoh/launcher.py": {"zeile": "line", "zeilen": "lines",
                            "zustand": "verdict_state",
                            "spec_pfad": "spec_file"},
}


def mapping_for(rel_path: str) -> dict[str, str]:
    return {**DEFAULT, **COMPOUNDS, **TEST_NAMES, **BULK, **OVERRIDES.get(rel_path, {})}


def _name_edits(text: str, mapping: dict[str, str]):
    """(line, col_start, col_end, replacement) for every NAME token to rename.

    A `NAME` token never spans lines, so an edit is always within one line and
    the slicing below cannot corrupt a multi-line construct.
    """
    edits = []
    for tok in tokenize.generate_tokens(io.StringIO(text).readline):
        if tok.type != tokenize.NAME:
            continue
        renamed = mapping.get(tok.string)
        if renamed is not None and renamed != tok.string:
            edits.append((tok.start[0], tok.start[1], tok.end[1], renamed))
    return edits


def bound_names(text: str) -> set[str]:
    """Every name this module binds: assignments, parameters, defs, imports.

    Attribute names are included -- `self.target` is a name in this file even
    though it lives on an object -- and keyword arguments in calls are not,
    because they belong to the callee.
    """
    tree = ast.parse(text)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.alias):
            names.add((node.asname or node.name).split(".")[0])
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            names.update(node.names)
    return names


def collisions(text: str, mapping: dict[str, str]) -> dict[str, str]:
    """German names whose English replacement already exists in this module.

    O189. `store.py` had a local `ziel` *and* a local `target` in one scope.
    Renaming `ziel -> target` collapsed two distinct variables into one, and
    the loop that followed rebuilt its own destination path on every iteration
    -- a nesting bug from a commit that claimed to change no behaviour. The
    suite caught it, and the suite catching it is luck: the same collision in
    a branch no test reaches would have shipped.

    Module scope, not function scope, and deliberately so. A function-scoped
    check would be more permissive and more nearly right, and "more nearly
    right" is the wrong property for the instrument that decides whether a
    rename is safe to make unreviewed. A false refusal costs one line in
    OVERRIDES; a false permission costs a silent behaviour change.

    What it does *not* count is a name this module never binds. The first
    version compared against every `NAME` token, which made
    `subprocess.run(..., check=True)` a reason to refuse renaming `pruefe` to
    `check` -- a keyword argument of somebody else's function is not a name in
    this file. Bindings are read from the syntax tree instead.
    """
    present = bound_names(text)
    return {
        german: english
        for german, english in mapping.items()
        # A name mapped to itself is a pin, not a rename: it is how a foreign
        # API's spelling is held in place, and it can never collide with
        # anything.
        if english != german and german in present and english in present
    }


def _argname_edits(text: str, mapping: dict[str, str]):
    """Edits for pytest argument names, which are strings and not identifiers.

    O190. The sweep renamed a test's parameter and left
    `@pytest.mark.parametrize("zustand", ...)` alone, because a string is not
    an identifier -- and pytest then refused to collect the module with
    `function uses no argument 'zustand'`. Three modules failed that way, at
    collection, which is the good case: a fixture name that no longer matches
    is a hard error rather than a silent skip.

    Narrow on purpose: only the `argnames` of `parametrize` and the names in
    `usefixtures`, located through the syntax tree. A general rewrite of
    strings that happen to look like identifiers is exactly what this tool
    exists not to do.
    """
    tree = ast.parse(text)
    edits = []

    def literals(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return [node]
        if isinstance(node, (ast.List, ast.Tuple)):
            return [e for e in node.elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)]
        return []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        if name in ("setattr", "getattr", "hasattr", "delattr") and len(node.args) >= 2:
            # `monkeypatch.setattr(mod, "digeste_im_commit", ...)` names an
            # attribute in a string. Twelve tests errored on exactly this
            # after the attribute itself was renamed. Only the second
            # argument, only a plain literal, and only a name the mapping
            # knows -- a general string rewrite is what this tool refuses to
            # be.
            targets_ = [lit for lit in literals(node.args[1])]
        elif name in ("parametrize", "usefixtures"):
            targets_ = literals(node.args[0]) if (name == "parametrize" and node.args) else [
                lit for a in node.args for lit in literals(a)]
        else:
            continue
        for lit in targets_:
            parts = [p.strip() for p in lit.value.split(",")]
            renamed = [mapping.get(p, p) for p in parts]
            if renamed == parts:
                continue
            edits.append((lit.lineno, lit.col_offset + 1,
                          lit.end_col_offset - 1, ",".join(renamed)))
    return edits


def rename_source(text: str, mapping: dict[str, str]) -> tuple[str, int]:
    """The source with identifiers renamed, and how many were renamed.

    Rebuilt by slicing the original lines, not by `untokenize`, which is free
    to reflow whitespace -- a rename commit that also reformats is a rename
    commit nobody can review.
    """
    edits = _name_edits(text, mapping) + _argname_edits(text, mapping)
    if not edits:
        return text, 0
    lines = text.splitlines(keepends=True)
    by_line: dict[int, list] = {}
    for lineno, a, b, renamed in edits:
        by_line.setdefault(lineno, []).append((a, b, renamed))
    for lineno, spots in by_line.items():
        line = lines[lineno - 1]
        for a, b, renamed in sorted(spots, reverse=True):
            line = line[:a] + renamed + line[b:]
        lines[lineno - 1] = line
    return "".join(lines), len(edits)


#: Word stems that make an identifier German. The mapping cannot answer this
#: question: it only knows the words somebody has already translated, so a
#: gate built on it would pass the moment the next function is named
#: `zeile_sonstwas`. This list is what makes the check about the language
#: rather than about the vocabulary -- and it is a list of stems, not a
#: dictionary, so it will miss words nobody here has used yet. That is a
#: narrow instrument with a stated boundary, which is the honest form.
GERMAN_PARTS = frozenset("""
lauf laeufe anker nachweis fremd fremde fremder prozess kontrolle je ohne nur
das ein eine der die dem den neuer neue ist annahme vorher keine
unentschieden ablehnung wirklich unabhaengige werden nicht aktionsklasse
kommt aus trockenlauf meldet und mergt nie unklar paritaet pruefer pruefung
pruefe zeile zeilen pfad pfade datei dateien quelle quellen ziel ziele grund
gruende kopf koerper offen zustand ergebnis ergebnisse befund befunde bericht
berichte knoten eintrag eintraege kandidat kandidaten abschnitt wurzel
wurzeln teile schluessel zaehler befehl antwort frage fehler eltern breite
luecke luecken nein verboten anerkannt geaendert damals jetzt gefunden fehlt
gesamt herkunft probleme betreff schreibe schreiber verifiziere waehle
anwenden nachziehen erwartet alle neu vorhanden kollision einmal zweimal
deutsch englisch funktionen benutzt arenen basis zellen zelle gruen leer
verankert starte fahre klassifiziere globale semantische abhaengigkeit
abbruch abdeckung abgelehnt abgerechnet abgesagt absage abweichend abweichung
aenderung aenderungen aktuell als alt alte anderer anderswo anfang anforderung
angefordert angenommen angewandt angriff anhaengen antwortdatei arbeit arbeiten
arbeitsbaum artefakt auf aufgabe aufgaben aufgeloest aufgezeichnet aufnahme
aufrufe ausfall ausfuehrer ausfuehrlich ausgabe ausgaben ausgang ausgeschlossen
aussen ausser ausserhalb baeume baum beantwortet bedingt bedingung behalten
behauptet bekannt beob beobachtet beratend bereich bereit beruehrt beschattet
beschreibung bestaetigt bestanden beste betroffen bewacht bewegt beweis beweist
binaererbeweis bindend bindungen blockiert braucht daten datensatz dauer dazu
dazwischen doppelt draussen dreck drin drinnen dritte drueber echt echte echter
eigen eigene eing eingeschlossen eingrenzung ende endzustand entdeckt entfernt
entscheidbar entschieden entsprechung entwertet entwickeln entwickler ereignisse
erforderliche erg erkannt erklaertes erlaubt erledigt erneuert ernst erreichbar
erschoepft erst erste erster erstes erzwingend faehrt faelle faelschen faelschung
falsch falsche falsifikator falsifikatoren falsifiziere feld felder fehlen
fehlend fern fertig fest festgeschrieben flach fluechtig folge fortschritt
fortsetzung frag freigabe frisch frisches frueher frueheste gebaut gebunden
gedriftet geehrt gefroren geheim gehoert gekuerzt gelaufen gelesen geliefert
gemeinsam gemessen gemischt genannt genommen geparkt geparkten gepflanzt geplant
geplante gescheitert geschlossen geschmuggelt geschrieben geschuetzt geschwister
gesehen gesetzt gespeichert gestaged gestartet gewuenscht gezaehlt gezaehlte
gleich grenze grenzen groesse gruppe gruppen gueltig gut gutes haette halte hart
haupt hier hindernis hindernisse hintergrund hintertuer hinzugefuegt historie
hoechste hoechstens holen identitaet identitaeten inhalt innen installieren
intern interne iterationen kampagne kampagnen kapsel kaputt kategorien keiner
kennung kette klasse klassen klettert kollidiert konfiguriert konform
konformitaet kontrollen kopie kopien kriterien kuenftig kurz laden laeuft lese
lief los melde meldung menschen menschlich merkend messen metadaten metrik modul
modus muss muster nach nachsatz naechste naiv namen nenne netz nimmt noetig notiz
notizen oben oder oeffnend ordner ort orte paare pflanzen pflanzungen plaene
planer planerin politik praefix prosa protokoll protokolle prototyp pruefen
pruefungen puffer punkt quittung quittungen raeder raetselhaft rang raus regel
rendern roh rolle rollen rot runde runden sammeln sammler sandkasten satz saetze
sauber schlecht schluss schmutz schmutzig schon schreib schreiben schreibfehler
schritt schritte schrott schwach sein signaturen skript sonst spaeter spanne
stapel stempel stirbt stopp stueck stuecke stumm subjekt suche summe tabelle teil
telemetrie transkript tun typ ueber ueberschreiben uebersprungen uebrig umfang
umgebung unabhaengig unbedingt unbekannt unbrauchbar unerklaert unerreichbar
ungueltig uns unser unsere unten unter unveraendert veraendert veraltet
verbleibendes verbrauchtes verbucht verbunden verdikt verdikte verfolgt vergleich
vergleiche verlangt verlauf verletzt verletzungen veroeffentlicht verschieden
verschwunden versender versionen versuch versuche verteilung verweigert verworfen
verzeichnis verzeichnisse viele voll vollstaendig von vorab vorbereiten
vorbestand vorgaenger vorsichtig wackelig wann wartet warum wechselnd weg weich
weit weiter welt werkzeug werkzeuge wert werte wiederholung wiederholungen wort
zaehlung zahl zeichen zeuge zuerst zuhause zurueck zurueckgehalten
zusammenfassung zusicherung zweig zweit zweite zweiter
""".split())


#: The second detector, and the reason there are two. A stem list can only
#: contain words somebody already knew about: the first version of this gate
#: reported zero German names while 521 were present, and the second reported
#: zero while 13 were present -- `_aggregatzeilen`, `LuegtUeberIsolation`,
#: `schlüssel`. Orthography catches what vocabulary cannot, because German
#: spells things English does not. It is noisy, so the words it fires on that
#: are ordinary English are listed out loud rather than silently tuned away.
_GERMAN_SPELLING = re.compile(r"(ae|oe|ue|sch|tz|zung|keit|heit|lich|isch|pf)")
_NOT_GERMAN = frozenset("""
schema schemas scheme schedule scheduler search searched searches matches batches
queue queues tuple tuples value values module modules cache caches offset offsets
buffer buffers differ differs differing staff stuff cutoff diff diffs effect
effects effective affect affects affected affecting unaffected suffix suffixes
prefix prefixes off offs truncate truncated quote quoted quotes true tz tzinfo
astimezone timezone hexdigest digest digests argue argues issue issues venue
continue continued unique queued dequeue due request requested requests question
questions sequence socket tempfile zipfile badzipfile valueerror getvalue packet
prologue backend backends backtick daemon different does goes guessed guessing
offender untracked coerced checker arenaescape blockedqa sandboxbackend
sequential subsequent consequence consequences frequency quest guest
language languages dialogue catalogue cheque conquer conquest
subsequently adequate adequately
""".split())


def is_german(name: str) -> bool:
    """Two detectors, and a name is German if either fires.

    Neither alone is enough: the vocabulary knows only what has already been
    found, and the spelling rule cannot tell `zung` in a German noun from
    `tz` in `astimezone`. Together they have caught everything two independent
    sweeps found, and the second one is what found what the first one missed.
    """
    parts = [p for p in re.split(r"[_\d]+", name.lower()) if p]
    if any(p in GERMAN_PARTS for p in parts):
        return True
    return any(len(p) > 2 and p not in _NOT_GERMAN and _GERMAN_SPELLING.search(p)
               for p in parts)


def remaining(text: str, mapping: dict[str, str]) -> dict[str, int]:
    """German NAME tokens still present, with counts.

    A name the mapping pins to itself is not counted: it is a foreign API's
    spelling or a field name in stored data, held in place on purpose, and a
    gate that reported it forever would be a gate nobody could get to green.
    """
    pinned = {g for g, e in mapping.items() if g == e}
    found_names: dict[str, int] = {}
    for tok in tokenize.generate_tokens(io.StringIO(text).readline):
        if tok.type != tokenize.NAME or tok.string in pinned:
            continue
        if tok.string in mapping or is_german(tok.string):
            found_names[tok.string] = found_names.get(tok.string, 0) + 1
    return dict(sorted(found_names.items()))


def _rel(p: Path) -> str:
    try:
        return str(p.resolve().relative_to(HOH))
    except ValueError:
        return str(p)


def _files(argv: list[str]) -> list[Path]:
    if not argv:
        argv = ["src", "tools", "tests"]
    out: list[Path] = []
    for a in argv:
        p = Path(a)
        out += sorted(p.rglob("*.py")) if p.is_dir() else [p]
    return [p for p in out if p.is_file()]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("check", "plan", "apply"):
        s = sub.add_parser(name)
        s.add_argument("paths", nargs="*")
    args = ap.parse_args(argv)

    files = _files(args.paths)
    total = 0
    refused = 0
    for f in files:
        rel = _rel(f)
        text = f.read_text(encoding="utf-8")
        m = mapping_for(rel)
        if args.cmd == "check":
            found_names = remaining(text, m)
            if found_names:
                total += 1
                print(f"{rel}: {len(found_names)} name(s) {list(found_names)[:8]}")
            continue
        clash = collisions(text, m)
        if clash:
            # Refused, not resolved: the tool cannot know which of the two
            # meanings the existing name has.
            print(f"{rel}: REFUSED, {len(clash)} name(s) already exist "
                  f"here: " + ", ".join(f"{d} -> {e}"
                                        for d, e in clash.items())
                  + ". Add an override for this path and re-run.")
            total += 1 if args.cmd == "check" else 0
            refused += 1
            continue
        renamed, n = rename_source(text, m)
        if not n:
            continue
        total += n
        if args.cmd == "plan":
            print(f"{rel}: {n} rename(s) {list(remaining(text, m))[:8]}")
        else:
            f.write_text(renamed, encoding="utf-8")
            print(f"{rel}: {n} rename(s) applied")
    if args.cmd == "check":
        print(f"{total} file(s) still carry German identifiers")
        return 1 if total else 0
    print(f"{total} rename(s) {'planned' if args.cmd == 'plan' else 'applied'}"
          + (f"; {refused} file(s) refused for name collisions" if refused else ""))
    return 2 if refused else 0


if __name__ == "__main__":
    raise SystemExit(main())
