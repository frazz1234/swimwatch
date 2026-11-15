"""Texts shown to people (notifications, entity names, picture labels). English and French."""
from __future__ import annotations

LABEL_KEYS = ["swim", "near", "animal", "none"]

TEXT = {
    "en": {
        "labels": {"swim": "Swimming", "near": "Person nearby", "animal": "Animal", "none": "Nothing"},
        "kinds": {"person": "person", "dog": "dog", "cat": "cat", "bird": "bird"},
        "ask_title": "{name}: is someone swimming?",
        "in_water": "{n} in the water?",
        "near": "{n} near the pool",
        "animal": "animal",
        "unclear": "nothing clear",
        "ask_msg": "{time} · {what} · swimming {p}. Long-press to answer.",
        "ent_swim": "{name} swimming",
        "ent_count": "{name} people in the water",
        "ent_last": "{name} last analysis",
    },
    "fr": {
        "labels": {"swim": "Baignade", "near": "Personne à côté", "animal": "Animal", "none": "Rien"},
        "kinds": {"person": "personne", "dog": "chien", "cat": "chat", "bird": "oiseau"},
        "ask_title": "{name} : quelqu'un se baigne ?",
        "in_water": "{n} dans l'eau ?",
        "near": "{n} près de la piscine",
        "animal": "animal",
        "unclear": "rien de clair",
        "ask_msg": "{time} · {what} · baignade {p}. Appuie longtemps pour répondre.",
        "ent_swim": "{name} baignade",
        "ent_count": "{name} personnes dans l'eau",
        "ent_last": "{name} dernière analyse",
    },
}


def t(lang: str) -> dict:
    return TEXT.get(lang, TEXT["en"])
