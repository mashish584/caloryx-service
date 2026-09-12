"""Bulk catalog ingestion parsers (PRD §8 "Data sources") - pure file parsing
and validation; the DB writes (`meals.repository.bulk_insert_foods`) are out
of scope here, same no-live-DB convention as the rest of the suite.

Source files are written into `tmp_path` from the inline samples below, in
each source's real bulk-download layout.
"""
from __future__ import annotations

import gzip

import pytest

from meals.ingest import FoodRecord, validate
from meals.ingest.common import infer_state, normalize_unit
from meals.ingest.indb import parse_indb
from meals.ingest.off import parse_off
from meals.ingest.usda import map_category, parse_usda


def _write(directory, files):
    for name, content in files.items():
        (directory / name).write_text(content.strip() + "\n", encoding="utf-8")
    return str(directory)


def _by_ref(records):
    return {r.source_ref: r for r in records}


# -- USDA FoodData Central ---------------------------------------------------

_SR_LEGACY = {
    "food.csv": """
"fdc_id","data_type","description","food_category_id","publication_date"
"174289","sr_legacy_food","Hummus, commercial","16","2019-04-01"
"169756","sr_legacy_food","Rice, white, long-grain, regular, cooked","20","2019-04-01"
"900001","sr_legacy_food","Implausible macros","16","2019-04-01"
"2000001","foundation_food","Hummus, plain, lab","16","2021-04-01"
"3000001","sample_food","Hummus sample lot 7","16","2021-04-01"
""",
    "food_nutrient.csv": """
"id","fdc_id","nutrient_id","amount"
"1","174289","1008","166"
"2","174289","1003","7.9"
"3","174289","1004","9.6"
"4","174289","1005","14.3"
"5","174289","1079","6.0"
"6","174289","1093","379"
"7","169756","1008","130"
"8","169756","1003","2.69"
"9","169756","1004","0.28"
"10","169756","1005","28.17"
"11","900001","1008","500"
"12","900001","1003","60"
"13","900001","1004","10"
"14","900001","1005","60"
"15","2000001","2047","170"
"16","2000001","1003","7.5"
"17","2000001","1085","9.0"
"18","2000001","1050","15.0"
"19","3000001","1008","999"
"20","174289","1162","0.0"
""",
    "food_portion.csv": """
"id","fdc_id","seq_num","amount","measure_unit_id","portion_description","modifier","gram_weight"
"1","174289","1","1.0","9999","","tbsp","15.0"
"2","174289","2","1.0","9999","","cup","246.0"
"3","174289","3","1.0","9999","","cup, packed","250.0"
"4","169756","1","1.0","9999","","cup","158.0"
"5","169756","2","1.0","9999","","oz","28.35"
""",
    "measure_unit.csv": """
"id","name"
"1000","cup"
"9999","undetermined"
""",
    "food_category.csv": """
"id","code","description"
"16","1600","Legumes and Legume Products"
"20","2000","Cereal Grains and Pasta"
""",
}

_SURVEY = {
    "food.csv": """
"fdc_id","data_type","description","food_category_id","publication_date"
"2341234","survey_fndds_food","Hummus, plain","2804","2022-10-28"
""",
    # The Survey download keys food_nutrient by legacy nutrient *number*
    # (208 = energy), unlike SR Legacy's ids - nutrient.csv maps between them.
    "food_nutrient.csv": """
"id","fdc_id","nutrient_id","amount"
"1","2341234","208","177"
"2","2341234","203","7.35"
"3","2341234","204","10.4"
"4","2341234","205","15.2"
"5","2341234","269","0.27"
"6","2341234","301","41"
""",
    "nutrient.csv": """
"id","name","unit_name","nutrient_nbr","rank"
"1003","Protein","G","203","600.0"
"1004","Total lipid (fat)","G","204","800.0"
"1005","Carbohydrate, by difference","G","205","1110.0"
"1008","Energy","KCAL","208","300.0"
"2000","Total Sugars","G","269","1510.0"
"1087","Calcium, Ca","MG","301","5300.0"
""",
    "food_portion.csv": """
"id","fdc_id","seq_num","amount","measure_unit_id","portion_description","modifier","gram_weight"
"1","2341234","1","","9999","1 tablespoon","10205","15.0"
"2","2341234","2","","9999","Quantity not specified","90000","30.0"
"3","2341234","3","","9999","1/2 cup","10205","123.0"
""",
    "wweia_food_category.csv": """
"wweia_food_category","wweia_food_category_description"
"2804","Dips, gravies, other sauces"
""",
}


@pytest.fixture
def sr_records(tmp_path):
    return _by_ref(parse_usda(_write(tmp_path, _SR_LEGACY)))


def test_usda_maps_classic_nutrient_ids_and_keeps_missing_as_none(sr_records):
    hummus = sr_records["174289"]
    assert hummus.name == "Hummus, commercial"
    assert hummus.source == "USDA"
    assert (hummus.calories_kcal, hummus.protein_g, hummus.fat_g, hummus.carbs_g) == (166.0, 7.9, 9.6, 14.3)
    assert hummus.fiber_g == 6.0
    assert hummus.sodium_mg == 379.0
    assert hummus.sugar_g is None  # not in the source -> unknown, never 0
    assert validate(hummus) is None


def test_usda_foundation_falls_back_to_atwater_energy_summation_carbs_and_nlea_fat(sr_records):
    lab = sr_records["2000001"]
    assert (lab.calories_kcal, lab.carbs_g, lab.fat_g) == (170.0, 15.0, 9.0)


def test_usda_skips_non_food_sample_rows(sr_records):
    assert "3000001" not in sr_records


def test_usda_portions_become_serving_units_first_per_unit_wins(sr_records):
    units = {u.unit: (u.grams, u.type) for u in sr_records["174289"].serving_units}
    assert units == {"tbsp": (15.0, "HOUSEHOLD"), "cup": (246.0, "HOUSEHOLD")}
    # "oz" isn't a unit the chat grammar can produce - dropped, not stored.
    assert [u.unit for u in sr_records["169756"].serving_units] == ["cup"]


def test_usda_infers_state_and_category(sr_records):
    rice = sr_records["169756"]
    assert rice.default_state == "COOKED"
    assert rice.category == "GRAIN"
    assert sr_records["174289"].category == "PROTEIN"  # legumes
    assert sr_records["174289"].default_state == "UNSPECIFIED"


def test_usda_implausible_macros_fail_validation(sr_records):
    assert validate(sr_records["900001"]) == "macro_sum"


def test_usda_survey_quantity_not_specified_is_the_default_serving(tmp_path):
    [hummus] = parse_usda(_write(tmp_path, _SURVEY))
    assert hummus.default_serving_grams == 30.0
    assert hummus.sugar_g == 0.27
    units = {u.unit: u.grams for u in hummus.serving_units}
    assert units == {"tbsp": 15.0, "cup": 246.0}  # "1/2 cup" = 123g -> 246g per cup
    assert hummus.category is None


def test_usda_missing_food_csv_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        list(parse_usda(str(tmp_path)))


@pytest.mark.parametrize(
    "description,expected",
    [
        ("Vegetables and Vegetable Products", "VEGETABLE"),
        ("Fats and Oils", "OIL"),
        ("Salad dressings and vegetable oils", "DRESSING"),
        ("Dairy and Egg Products", None),
        ("Eggs and omelets", "PROTEIN"),
        ("Sweets", None),
        (None, None),
    ],
)
def test_usda_category_mapping(description, expected):
    assert map_category(description) == expected


# -- INDB ----------------------------------------------------------------------

_INDB = """
food_code,food_name,energy_kcal,carb_g,protein_g,fat_g,fibre_g,freesugar_g,sfa_mg,sodium_mg,cholesterol_mg,servings_unit,unit_serving_energy_kcal
ASC171,Dal makhani,140,12,5,8,3,1,4500,300,10,katori,210
A001,"Rice, raw, milled",356,78,7,0.5,0.2,,150,5,0,,
,Row without a code,100,10,10,1,,,,,,,
"""


def test_indb_parses_per_100g_values_and_converts_sfa_mg_to_g(tmp_path):
    path = tmp_path / "INDB.csv"
    path.write_text(_INDB.strip() + "\n", encoding="utf-8")
    records = _by_ref(parse_indb(str(path)))

    assert set(records) == {"ASC171", "A001"}
    dal = records["ASC171"]
    assert dal.source == "INDB"
    assert (dal.calories_kcal, dal.protein_g, dal.carbs_g, dal.fat_g) == (140.0, 5.0, 12.0, 8.0)
    assert dal.saturated_fat_g == 4.5
    assert dal.sugar_g is None  # free sugar is not total sugar - not mapped
    assert validate(dal) is None


def test_indb_derives_grams_per_household_serving_from_serving_energy(tmp_path):
    path = tmp_path / "INDB.csv"
    path.write_text(_INDB.strip() + "\n", encoding="utf-8")
    records = _by_ref(parse_indb(str(path)))

    dal = records["ASC171"]
    assert [(u.unit, u.grams, u.type) for u in dal.serving_units] == [("katori", 150.0, "HOUSEHOLD")]
    assert dal.default_serving_grams == 150.0
    rice = records["A001"]
    assert rice.serving_units == []
    assert rice.default_state == "RAW"


# -- Open Food Facts -----------------------------------------------------------

_OFF_HEADER = [
    "code", "product_name", "brands", "countries_tags", "energy-kcal_100g", "proteins_100g",
    "carbohydrates_100g", "fat_100g", "fiber_100g", "sugars_100g", "saturated-fat_100g",
    "sodium_100g", "cholesterol_100g", "serving_quantity", "unique_scans_n",
]
_OFF_ROWS = [
    ["8901", "Classic Hummus", "Brand A,Brand B", "en:india", "170", "8", "14", "10", "", "1", "1.5", "0.4", "", "30", "12"],
    ["8902", "US Hummus", "Brand C", "en:united-states", "170", "8", "14", "10", "", "", "", "", "", "", "50"],
    ["8903", "No fat listed", "", "en:india,en:france", "100", "5", "10", "", "", "", "", "", "", "", "9"],
    ["8904", "kJ typed as kcal", "", "en:india", "700", "5", "10", "5", "", "", "", "", "", "", "9"],
    ["8905", "Rarely scanned", "", "en:india", "120", "4", "20", "2", "", "", "", "", "", "", "1"],
    ["8906", "", "", "en:india", "120", "4", "20", "2", "", "", "", "", "", "", "9"],
]


@pytest.fixture
def off_path(tmp_path):
    path = tmp_path / "products.csv.gz"
    lines = ["\t".join(_OFF_HEADER)] + ["\t".join(row) for row in _OFF_ROWS]
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    return str(path)


def test_off_is_global_by_default_and_requires_a_name(off_path):
    refs = set(_by_ref(parse_off(off_path)))
    assert refs == {"8901", "8902", "8903", "8904", "8905"}


def test_off_countries_narrows_to_tagged_products(off_path):
    refs = set(_by_ref(parse_off(off_path, countries=["en:india"])))
    assert refs == {"8901", "8903", "8904", "8905"}
    assert "8902" in _by_ref(parse_off(off_path, countries=["en:india", "en:united-states"]))


def test_off_min_scans(off_path):
    assert "8905" not in _by_ref(parse_off(off_path, min_scans=5))


def test_off_maps_brand_units_and_serving(off_path):
    hummus = _by_ref(parse_off(off_path))["8901"]
    assert hummus.source == "OPEN_FOOD_FACTS"
    assert hummus.brand == "Brand A"
    assert hummus.sodium_mg == pytest.approx(400.0)  # g/100g -> mg
    assert hummus.fiber_g is None
    assert hummus.default_serving_grams == 30.0
    assert validate(hummus, check_energy=True) is None


def test_off_validation_catches_missing_macro_and_energy_mismatch(off_path):
    records = _by_ref(parse_off(off_path))
    assert validate(records["8903"], check_energy=True) == "missing_macro"
    assert validate(records["8904"], check_energy=True) == "kcal_mismatch"


# -- shared validation / heuristics -------------------------------------------


def _record(**overrides):
    fields = dict(
        name="Food", source="USDA", source_ref="1",
        calories_kcal=100.0, protein_g=5.0, carbs_g=15.0, fat_g=2.0,
    )
    fields.update(overrides)
    return FoodRecord(**fields)


def test_validate_rejects_negative_and_blank_name():
    assert validate(_record(fiber_g=-5.0)) == "negative_value"
    assert validate(_record(name="  ")) == "missing_name"


def test_validate_energy_check_is_opt_in_and_ignores_low_energy_foods():
    beer = _record(calories_kcal=43.0, protein_g=0.5, carbs_g=3.6, fat_g=0.0)  # alcohol kcal
    assert validate(beer) is None
    assert validate(beer, check_energy=True) == "kcal_mismatch"
    lettuce = _record(calories_kcal=15.0, protein_g=1.4, carbs_g=2.9, fat_g=0.2)
    assert validate(lettuce, check_energy=True) is None


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Chicken breast, grilled", "COOKED"),
        ("Rice, white, raw", "RAW"),
        ("Rice, raw and cooked blend", "UNSPECIFIED"),
        ("Hummus", "UNSPECIFIED"),
        ("Stir-fried vegetables", "COOKED"),
        ("Strawberry", "UNSPECIFIED"),
    ],
)
def test_infer_state(name, expected):
    assert infer_state(name) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("cup, chopped", "cup"),
        ("Tablespoons", "tbsp"),
        ("slice", "slice"),
        ("katori", "katori"),
        ("oz", None),
        ("g", None),  # universal - never stored per food
        ("large", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_unit(text, expected):
    assert normalize_unit(text) == expected


# -- manage.py ingest_foods (repository stubbed) -------------------------------

from io import StringIO  # noqa: E402

from django.core.management import call_command  # noqa: E402
from django.core.management.base import CommandError  # noqa: E402

from meals import repository  # noqa: E402


@pytest.fixture
def writes(monkeypatch):
    state = {"batches": [], "upserts": [], "bumps": 0}

    def bulk_insert_foods(records):
        state["batches"].append([r.source_ref for r in records])
        return len(records)

    def bump_catalog_version():
        state["bumps"] += 1
        return 7

    monkeypatch.setattr(repository, "bulk_insert_foods", bulk_insert_foods)
    monkeypatch.setattr(repository, "upsert_food", lambda r: state["upserts"].append(r.source_ref))
    monkeypatch.setattr(repository, "bump_catalog_version", bump_catalog_version)
    return state


def _run(*args):
    out = StringIO()
    call_command("ingest_foods", *args, stdout=out)
    return out.getvalue()


def test_ingest_command_writes_valid_records_tallies_skips_and_bumps_catalog(tmp_path, writes):
    out = _run("--source", "usda", "--path", _write(tmp_path, _SR_LEGACY))

    assert sorted(writes["batches"][0]) == ["169756", "174289", "2000001"]
    assert "skipped (macro_sum): 1" in out
    assert "written: 3" in out
    assert writes["bumps"] == 1


def test_ingest_command_dry_run_writes_nothing(tmp_path, writes):
    out = _run("--source", "usda", "--path", _write(tmp_path, _SR_LEGACY), "--dry-run")

    assert writes["batches"] == [] and writes["bumps"] == 0
    assert "valid: 3" in out


def test_ingest_command_off_uses_energy_check_and_limit(off_path, writes):
    out = _run("--source", "off", "--path", off_path, "--limit", "1")

    assert writes["batches"] == [["8901"]]
    assert "valid: 1" in out


def test_ingest_command_update_existing_upserts_row_by_row(tmp_path, writes):
    _run("--source", "usda", "--path", _write(tmp_path, _SR_LEGACY), "--update-existing")

    assert sorted(writes["upserts"]) == ["169756", "174289", "2000001"]
    assert writes["batches"] == []


def test_ingest_command_missing_path_is_a_command_error(tmp_path, writes):
    with pytest.raises(CommandError):
        _run("--source", "indb", "--path", str(tmp_path / "nope.csv"))


def test_search_like_pattern_escapes_wildcards():
    assert repository._escape_like("50%_off\\") == "50\\%\\_off\\\\"


def test_negligible_negative_nutrients_read_as_zero():
    # USDA computes carbs "by difference" and underflows on raw meats -
    # Foundation lists chicken breast at -0.43g carbs.
    chicken = _record(calories_kcal=132.8, protein_g=21.4, carbs_g=-0.43, fat_g=4.8)
    assert chicken.carbs_g == 0.0
    assert validate(chicken) is None


def test_real_negative_values_still_fail_validation():
    assert validate(_record(carbs_g=-4.0)) == "negative_value"
    # -1.0 is the tolerance boundary: clamped, not rejected.
    assert _record(carbs_g=-1.0).carbs_g == 0.0
    assert validate(_record(carbs_g=-1.001)) == "negative_value"
