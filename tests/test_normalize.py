"""The model classifier, on real titles encountered (brief) and on the traps found while scanning."""
import pytest

# ---- brief cases: (title, expected verdict, expected family or reject reason)
BRIEF_CASES = [
    ("Toshiba MG05ACA800E 8 To SATA III 7200 tr/min", "accept", "Toshiba MG"),
    ("Seagate IronWolf ST8000VN004 8Tb (9000h environ, évolutif)", "accept", "IronWolf"),
    ("Disque dur Seagate 8TB IronWolf ST8000VN0022 — Pour pièces détachées", "reject", "dead"),
    ("Disque dur interne Barracuda 8To ST8000DM004", "reject", "smr"),
    ("Hitachi HUH728080AL4200 8TB SAS", "reject", "sas"),
    ("Seagate Exos 7E8 ST8000NM0055 512e 256MB", "accept", "Exos"),
    ("Disque dur 8to 10to 12to 16to neuf et garantie — 140 €", "reject", "generic"),
    ("Disque dur externe Seagate 8to USB3", "reject", "external"),
    ("WD Red Pro WD8005FFBX 8 To 7200 RPM", "accept", "WD Red Pro"),
]


@pytest.mark.parametrize("title,verdict,expect", BRIEF_CASES)
def test_brief_cases(classifier, title, verdict, expect):
    info = classifier.classify(title)
    assert info.verdict == verdict, info
    if verdict == "accept":
        assert info.family == expect, info
    else:
        assert expect in info.reasons[0], info


def test_brief_model_unknown(classifier):
    info = classifier.classify("Disque dur nas 8 to (aucune réf.)")
    assert info.accepted
    assert "model_unknown" in info.flags
    assert info.model is None


# ---- real titles seen on leboncoin during development
REAL_CASES = [
    ("Disque dur 8To - Seagate Barracuda [HS]", "reject", "dead"),
    ("Disque dur ssd sasmsung 8to", "reject", "ssd"),
    ("Disque dur Xbox Seagate 8to", "reject", "external"),
    ("Disque dur interne SAS 8To HGST 12 Gb/s 7200 RPM", "reject", "sas"),
    ("Lot de 3 disques durs 8To sas (Roben)", "reject", "sas"),
    ("Disques dur Western Digital 1,2,3,4,8 TO", "reject", "generic"),
    ("Disques durs multiples capacités (80go, 500go, 1to, 2to, 8to, 16to)", "reject", "generic"),
    ("Lot disque dure hdd 3.5 de 500gb a 3to", "reject", "capacity_mismatch"),
    ("Disque dur G-RAID 8To Thunderbolt – Testé – Pro vidéo / Mac", "reject", "external"),
    ("Adaptateurs 2\"1/2 pour caddy 3\"1/2", "reject", "2.5in"),
    ("Enregistreur caméras IP - NVR D-Link DNR-4020-16P – PoE 16 canaux", "reject", "nas_bundle"),
    ("Serveur NAS Synology DS124 avec 1x disque dur 8To IRONWOLF", "reject", "nas_bundle"),
    ("QNAP TS-419p II en excellent état avec stockage 8To Samsung Barracuda", "reject", "nas_bundle"),
    ("NAS Synology DS414J", "reject", "nas_bundle"),
    ("Nas synology rackstation RS814 1u 4 baies 3,5\" rack server", "reject", "nas_bundle"),
    ("Disque dur interne Barracuda 8To ST800DMZ04", "reject", "smr"),
    ("Disque dur Seagate BarraCuda Compute 8TO 3,5\" SATA HDD", "reject", "smr"),
    ("Disque dur externe WD My Book 8 To, USB 3.0, faible usage", "reject", "external"),
    ("Hdd disque dur portable seagate 8to tb", "reject", "external"),
    ("Disque dur externe 8000 gigas (8to)", "reject", "external"),
    ("Disque dur DELL 1.8TB 10K SAS 12GB", "reject", "sas"),
    ("Disque dur 4 To Seagate IronWolf ST4000VN008", "reject", "capacity_mismatch"),
    ("UGREEN Docking Station Hub USB C et Support pour Mac Mini M4", "reject", "external"),
]


@pytest.mark.parametrize("title,verdict,expect", REAL_CASES)
def test_real_rejects(classifier, title, verdict, expect):
    info = classifier.classify(title)
    assert info.verdict == verdict, info
    assert expect in info.reasons[0], info


ACCEPT_CASES = [
    ("Disque dur 8to 3''5 WD Gold WD8004FRYZ SATA III - DataCenter/Serveur/Nas", "WD Gold", "WD8004FRYZ", []),
    ("Disque dur interne 8To, Seagate IronWolf", "IronWolf", None, ["ref_missing"]),
    ("Disque dur 8To - Seagate Exos 7E8 - SATA Enterprise", "Exos", None, ["ref_missing"]),
    ("Disque dur Seagate 8To Exos 78E", "Exos", None, ["ref_missing"]),
    ("Disque dur western digital Purple 8To", "WD Purple", None, ["ref_missing", "low_tier"]),
    ("Disque dur Nas 8To Toshiba", None, None, ["model_unknown"]),
    ("Disque dur 8 To 7200t SATA HP", None, None, ["model_unknown"]),
    ("Seagate ST8000VN004 8 TB", "IronWolf", "ST8000VN004", []),
    ("HGST Ultrastar He8 HUH728080ALE600 8TB SATA 512e", "Ultrastar", "HUH728080ALE600", []),
    ("WD Red Plus 8TB WD80EFZZ NASware", "WD Red Plus", "WD80EFZZ", []),
    ("Toshiba N300 8TB HDWG480UZSVA", "Toshiba N300", "HDWG480", []),
    ("Toshiba MG08ADA800E 8TB Enterprise", "Toshiba MG", "MG08ADA800E", []),
    ("Seagate SkyHawk ST8000VX004 8To surveillance", "SkyHawk", "ST8000VX004", ["low_tier"]),
    ("Seagate Exos ST8000NM0105 8TB SED", "Exos", "ST8000NM0105", ["ref_unverified"]),
    ("WD 8TB WD80EMAZ white label helium", "WD white label", "WD80EMAZ", ["ref_unverified"]),
    ("Disque dur 8 To ST8000 VN004 IronWolf", "IronWolf", "ST8000VN004", []),
    ("Disque dur 8 To ST8000-VN004", "IronWolf", "ST8000VN004", []),
]


@pytest.mark.parametrize("title,family,model,flags", ACCEPT_CASES)
def test_real_accepts(classifier, title, family, model, flags):
    info = classifier.classify(title)
    assert info.accepted, info
    assert info.family == family, info
    assert info.model == model, info
    for f in flags:
        assert f in info.flags, info
    if not flags:
        assert not ({"model_unknown", "ref_missing", "ref_unverified", "low_tier"} & set(info.flags)), info


# ---- structural rules on references missing from the catalogue
@pytest.mark.parametrize("ref,verdict,reason", [
    ("HUH728080ALN600", "reject", "4kn"),
    ("HUH728080AL5200", "reject", "sas"),
    ("HUS728T8TAL5204", "reject", "sas"),
    ("HUS728T8TALE6L4", "accept", None),
    ("ST8000NM0045", "reject", "4kn"),
    ("ST8000NM0075", "reject", "sas"),
    ("ST8000NM0065", "reject", "sas"),
    ("ST8000AS0002", "reject", "smr"),
    ("ST8000DM005", "reject", "smr"),
    ("WD80EDAZ", "reject", "smr"),
    ("WD80EAZZ", "reject", "smr"),
    ("MG08SCA800E", "reject", "sas"),
    ("MG06ACA800A", "reject", "4kn"),
    ("MG06ACA800E", "accept", None),
    ("MN08ADA800E", "accept", None),
])
def test_structural_refs(classifier, ref, verdict, reason):
    info = classifier.classify(f"Disque dur 8 To {ref}")
    assert info.verdict == verdict, info
    if reason:
        assert info.reasons[0] == reason, info
    else:
        assert info.model == ref


# ---- extraction of side information
def test_lot_quantity_and_hours(classifier):
    info = classifier.classify("Lot de 2 Disques dur Seagate Exos 7E8 8To", "env 13000h d'usage, SMART OK")
    assert info.accepted and info.quantity == 2 and "lot" in info.flags
    assert info.attrs.get("smart_hours") == 13000


def test_lot_total_capacity_not_a_competing_capacity(classifier):
    info = classifier.classify("2x 8To Seagate IronWolf ST8000VN004 (16To au total)")
    assert info.accepted and info.quantity == 2
    assert "multi_capacity" not in info.flags


def test_hours_from_title(classifier):
    info = classifier.classify("Seagate IronWolf ST8000VN004 8Tb (9000h environ, évolutif)")
    assert info.attrs.get("smart_hours") == 9000


def test_power_on_hours_english(classifier):
    info = classifier.classify("WD Red 8TB WD80EFAX", "Power-On Hours: 21 345, no reallocated sectors")
    assert info.attrs.get("smart_hours") == 21345
    assert info.accepted


def test_negated_dead_keyword_is_not_dead(classifier):
    info = classifier.classify("Seagate Exos ST8000NM0055 8To", "Aucun secteur défectueux, 0 reallocated, 100% fonctionnel")
    assert info.accepted, info


def test_dead_in_description_rejects(classifier):
    info = classifier.classify("Seagate Exos ST8000NM0055 8To", "Vendu pour pièces, ne fonctionne plus")
    assert not info.accepted and info.reasons == ["dead"]


def test_condition_parts_rejects(classifier):
    info = classifier.classify("Seagate Exos ST8000NM0055 8To", condition_code="parts")
    assert not info.accepted and info.reasons == ["dead"]


def test_sas_in_description_only_without_catalog_ref(classifier):
    assert not classifier.classify("Disque 8To entreprise", "Interface SAS 12Gb/s").accepted
    # a catalogue SATA reference wins over a "SAS" in the description (e.g. "SATA, pas SAS")
    assert classifier.classify("Seagate Exos ST8000NM0055", "SATA 6Gb/s (pas SAS)").accepted


def test_mpn_from_ebay_description(classifier):
    info = classifier.classify("Seagate 8TB 3.5 Enterprise HDD", "MPN: ST8000NM000A\nCapacity: 8 TB")
    assert info.model == "ST8000NM000A" and info.family == "Exos"


def test_smr_keyword_explicit_cmr(classifier):
    assert classifier.classify("Disque 8To CMR (pas SMR) IronWolf").accepted
    assert not classifier.classify("Disque 8To SMR archive").accepted


def test_capacity_regex_ignores_speed_and_hours(classifier):
    info = classifier.classify("Disque dur 8 To 7200t SATA 12 Gb/s 5000h")
    # 12 Gb/s → SAS; here we only check capacities and hours
    assert info.attrs.get("capacities_tb", []) == [8.0] or "sas" in info.reasons
    info = classifier.classify("Disque dur 8 To 7200t SATA 5000h")
    assert info.attrs.get("capacities_tb", []) == [8.0] and info.attrs.get("smart_hours") == 5000


# ---- traps found by the first real probe under Docker (9 September 2026)
@pytest.mark.parametrize("title,verdict,expect", [
    ("Disque dur Seagate 7E 2000", "reject", "capacity_mismatch"),        # Exos 7E2000 = 2 TB
    ("Hard drive plusieurs modèles", "reject", "generic"),
    ("Disque dur seagate", "reject", "no_capacity"),
    ("Disque dur Seagate Exos X16", "reject", "capacity_mismatch"),
])
def test_probe_rejects(classifier, title, verdict, expect):
    info = classifier.classify(title)
    assert info.verdict == verdict and expect in info.reasons[0], info


@pytest.mark.parametrize("title,qty", [
    ("4x Seagate Exos 7E8", 4),
    ("3 Seagate Exos 7E8 pour Theophile", 3),
    ("Dell enterprise plus Exos 7E8 8To x2", 2),
    ("HDD SEAGATE Exos 8 To", 1),
])
def test_probe_lots_and_exos_name_capacity(classifier, title, qty):
    info = classifier.classify(title)
    assert info.accepted and info.family == "Exos" and info.quantity == qty, info
    assert 8.0 in info.attrs.get("capacities_tb", [])


def test_inch_marker_is_not_a_lot(classifier):
    # "3''5" = 3.5 inches, not a lot of 5 (seen on a real WD Gold listing)
    info = classifier.classify("Disque dur 8to 3''5 WD Gold WD8004FRYZ SATA III")
    assert info.accepted and info.quantity == 1 and "lot" not in info.flags


# ---- false positives found during the first full production scan (11 searches, 807 listings)
@pytest.mark.parametrize("title,expect", [
    ("HDD Seagate Exos 7E8 4 To SATA Enterprise", "capacity_mismatch"),        # 7E8 = family, 4 TB = the drive
    ("Disque dur 3\"5 Seagate Exos 7E8 S-ATA III 6 To ST6000NM021A", "capacity_mismatch"),
    ("Seagate Exos 7E8 4to HDD", "capacity_mismatch"),
    ("Lot 16 disques durs de 500 Go, soit 8 To  SATA - 3.5\"", "capacity_mismatch"),
    ("🟡 8x Disque dur 3,5\" (total 8 To)", "capacity"),         # no_capacity or capacity_mismatch: rejected
    ("NAS Buffalo TeraStation 5410RN 4x8To 10Gbe", "nas_bundle"),
    ("Wd book 8 tb", "external"),
    ("Disque dur 8To USB3", "external"),
])
def test_production_false_positives(classifier, title, expect):
    info = classifier.classify(title)
    assert not info.accepted and expect in info.reasons[0], info


def test_exos_name_still_counts_when_nothing_else(classifier):
    assert classifier.classify("4x Seagate Exos 7E8").accepted
    info = classifier.classify("Lot de 2 Disques dur Seagate Exos 7E8 8To (16 To au total)")
    assert info.accepted and info.quantity == 2, info


@pytest.mark.parametrize("title,expect", [
    ("Hdd Seagate 3.5 8To en parfait état fonctionnel. Compatible NAS ST6000AS0002", "smr"),   # the reference wins over the "8To"
    ("NAS WD MyCloud 8 Tb", "nas_bundle"),
])
def test_production_false_positives_second_pass(classifier, title, expect):
    info = classifier.classify(title)
    assert not info.accepted and expect in info.reasons[0], info


def test_external_brand_names(classifier):
    for t in ("NEUF 8 to idéal Apple (ou pc) Quattro 3.0", "G-Drive 8TB Thunderbolt", "Freecom 8To"):
        info = classifier.classify(t)
        assert not info.accepted and info.reasons == ["external"], (t, info)
