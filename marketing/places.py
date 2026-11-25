"""Backgrounds ("settings") for cartoon slides: presets by product, chosen per slide by the AI script step, editable as free text.
The setting text is always English (it goes to the image model); Hindi stays in dialogue and narration only."""
import re

# key -> (label shown in the editor, English description used in the picture prompt)
PRESETS = {
    "lab_reception": ("Lab reception desk", "pathology laboratory reception desk with registers, a counter, a computer and a waiting chair"),
    "sample_collection": ("Sample collection room", "sample collection room of a pathology lab with a chair, a tray and sample tubes in racks"),
    "lab_bench": ("Lab bench", "pathology lab bench with sample tubes in racks, a microscope and a blood analyser machine"),
    "report_handover": ("Report handover counter", "lab reception counter where a printed report folder is handed over, registers and a computer on the desk"),
    "lab": ("Pathology lab", "pathology laboratory interior with sample tubes, a microscope, an analyser, a lab bench and a reception desk with registers in the background"),
    "clinic_reception": ("Clinic reception", "clinic reception with a front desk, a computer and a visitor bench"),
    "hospital_reception": ("Hospital reception", "hospital reception hall with a long front desk, registers, a computer and a few chairs"),
    "doctor_room": ("Doctor's room", "doctor's consultation room with a desk, two chairs and an examination couch"),
    "waiting_area": ("Waiting area", "hospital waiting area with rows of chairs and a reception counter far behind"),
    "retail_shop": ("Kirana / retail shop", "small Indian kirana shop interior with a wooden counter and shelves of packets"),
    "billing_counter": ("Billing counter", "shop billing counter with a computer, a receipt printer and shelves behind"),
    "godown": ("Warehouse / godown", "warehouse godown with stacked cartons and sacks on racks"),
    "office": ("Office", "small modern office with desks, computers and a window"),
    "street": ("Street outside the building", "street outside a building with a plain entrance and a blank signboard"),
    "phone_closeup": ("Phone-screen close-up", "close-up of a hand holding a smartphone, simple blurred background"),
}
# which drawn fallback scene (SVG parts) to use when no AI picture is available
SVG_FOR = {"retail_shop": "shelves", "billing_counter": "shop_counter", "godown": "shelves", "street": "shop_front", "phone_closeup": "plain", "office": "plain"}

FAMILIES = {
    "lims": ["lab_reception", "sample_collection", "lab_bench", "report_handover", "office"],
    "hms": ["hospital_reception", "doctor_room", "waiting_area", "clinic_reception", "office"],
    "inventory": ["retail_shop", "billing_counter", "godown", "retail_shop", "office"],
}
GENERIC = ["office", "retail_shop", "billing_counter", "street", "office"]
DEVANAGARI = re.compile(r"[ऀ-ॿ]")


def family(product_name):
    n = (product_name or "").lower()
    for key in ("lims", "hms", "inventory"):
        if key in n:
            return key
    return ""


def defaults(product_name):
    """Preset keys that fit the product, in the order a story usually visits them."""
    return FAMILIES.get(family(product_name), GENERIC)


def menu(product_name):
    """Presets for the editor: the product's own first, then the rest."""
    first = defaults(product_name)
    keys = list(dict.fromkeys(first + [k for k in PRESETS if k not in first]))
    return [(k, PRESETS[k][0]) for k in keys]


def text_of(key):
    return PRESETS[key][1] if key in PRESETS else ""


def english_only(text, limit=300):
    """Setting and picture descriptions are English: Devanagari is removed; text that was mostly Devanagari becomes ''."""
    t = str(text or "")
    if not t.strip():
        return ""
    dev = len(DEVANAGARI.findall(t))
    if dev and dev * 2 >= len(re.sub(r"\s", "", t)):
        return ""
    return " ".join(DEVANAGARI.sub("", t).split())[:limit]


def default_for(product_name, i):
    ks = defaults(product_name)
    return ks[i % len(ks)]


def prompt_hint(product_name):
    """Text for the script prompt: the settings that match the product, with their descriptions."""
    ks = defaults(product_name)
    return "\n".join("  %s: %s" % (k, PRESETS[k][1]) for k in dict.fromkeys(ks + ["office", "street", "phone_closeup"]))
