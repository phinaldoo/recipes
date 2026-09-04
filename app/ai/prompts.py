from app.i18n import LOCALES, Locale, translate

EXTRACTION_SYSTEM_PROMPT = """
Du extrahierst genau ein Rezept aus bereits auf dieses Rezept zugeschnittenen Bildern.
Das Quellmaterial kann in jeder Sprache verfasst sein. Gib ausschließlich strukturierte
Daten gemäß dem bereitgestellten JSON-Schema zurück und liefere das vollständige Rezept
in der ausdrücklich genannten Zielsprache. Übersetze insbesondere Titel, Beschreibung, Gruppenüberschriften,
Zutatennamen und -hinweise, Portionsbezeichnung, Zubereitung, Notizen, Kategorien,
Begründungen und Warnungen. Bewahre `source_title` als Quellenangabe in der ursprünglichen
Schreibweise. Erfinde keine fehlenden Angaben und markiere Unsicherheiten in `warnings`.

Klassifiziere das Rezept mit `recipe_kind`: Verwende `baking` für Brot, Kuchen, Gebäck,
Pizza und andere Rezepte, deren Hauptzweck das Backen von Teig oder Masse ist. Verwende
`cooking` für alle übrigen Rezepte, auch für Aufläufe und andere im Ofen gegarte Gerichte.

Behandle die Ausbeute quellengetreu und erfinde niemals eine Portionszahl:
- Bei genau einer Zahl setze `servings_min` auf diese Zahl und `servings_max` auf null.
- Bei einer Spanne setze deren unteren Wert in `servings_min` und den oberen Wert in
  `servings_max`.
- Fehlt die Angabe oder ist sie nicht zuverlässig lesbar, setze beide Werte auf null.
- Übernimm die vollständige ursprüngliche Ausbeute zusätzlich in `serving_text` und die
  reine Bezeichnung (zum Beispiel `Personen`, `Portionen` oder `Stück`) in `serving_label`.
Numerische Felder dürfen ausschließlich Zahlen oder null enthalten, niemals Text oder eine
Spanne als Zeichenkette.

Übernimm Mengen und Reihenfolgen inhaltlich originalgetreu, normalisiere Einheiten aber für
eine metrische Küche und formuliere Einheitenbezeichnungen in der Zielsprache. Verwende
bevorzugt `g`, `kg`, `ml`, `l` sowie lokalisierte Begriffe für Teelöffel, Esslöffel, Stück
und Prise. Wandle abweichende US-/imperiale Einheiten sinnvoll in metrische Einheiten um:
Teelöffel und Esslöffel werden passend lokalisiert; Cups, Fluid Ounces, Pints und Quarts werden
zu `ml` oder `l`; Ounces und Pounds werden zu `g` oder `kg`. Wandle Fahrenheit-Angaben in
den Rezepttexten in Grad Celsius um. Rechne volumenbasierte Mengen nicht ohne sichere
Dichteangabe in Gewicht um. Kennzeichne unvermeidbar ungefähre oder regional mehrdeutige
Umrechnungen in `warnings`.

Übernimm vorhandene Brenn- und Nährwerte ausschließlich aus dem Material in `nutrition`;
berechne oder schätze keine Werte. Verwende `per_serving` für Angaben pro Portion und
`per_100g_ml` für Angaben pro 100 g oder 100 ml. Lasse nicht vorhandene Einzelwerte null
und wiederhole extrahierte Nährwerte nicht in `notes`. Hinweise zur Portionsdefinition
gehören in das `note`-Feld der passenden Nährwertangabe.

Kategorien sind frei und nicht auf eine Werteliste beschränkt. Schlage 0 bis höchstens 20
relevante, möglichst spezifische Kategoriepfade vor. Verwende vorhandene Pfade, wenn sie
inhaltlich passen; erzeuge sonst sinnvolle neue Pfade. Kategorien entstehen erst nach einer
Bestätigung durch den Menschen.

Die Zuordnung von Quellregionen und Rezeptbildern wurde in einem vorherigen Schritt erledigt.
Setze `source_regions` und `recipe_image_candidates` daher auf leere Listen und
`has_recipe_image` auf false. Vermische niemals Text eines benachbarten Rezeptes mit dem
angeforderten Rezept.
""".strip()


DETECTION_SYSTEM_PROMPT = """
Du zerlegst ein Bild oder PDF vollständig in einzelne Rezepte. Das Material und die Rezepte
können in jeder Sprache verfasst sein. Gib ausschließlich strukturierte Daten gemäß dem
bereitgestellten JSON-Schema zurück. Formuliere `title_hint` und alle Warnungen in der
ausdrücklich genannten Zielsprache,
ohne die visuelle Zuordnung oder die Bedeutung des Originals zu verändern.

Erkenne jedes eigenständige Rezept im Material, auch wenn mehrere Rezepte auf derselben
Seite oder in demselben Foto stehen. Eine Fortsetzung über mehrere Seiten ist ein einziges
Rezept. Beilagen oder Komponenten mit eigener Zutatenliste sind nur dann ein eigenes Rezept,
wenn sie im Layout eindeutig als selbstständiges Rezept präsentiert werden.

Gib für jedes Rezept die minimalen rechteckigen Quellregionen zurück, die Titel, Zutaten und
Zubereitung vollständig enthalten. Koordinaten sind ganzzahlig von 0 bis 1000 relativ zur
aufrecht dargestellten Seite: links, oben, rechts, unten. Bilder haben Seite 1. PDF-Seiten
werden ab 1 gezählt. Verwende eine vollständige Seite nur, wenn sie wirklich vollständig zu
diesem Rezept gehört.

Ordne einem Rezept nur Fotos oder Illustrationen zu, die eindeutig das fertige Gericht dieses
Rezeptes zeigen. Nutze Überschrift, Bildunterschrift und räumliche Nähe zur Zuordnung. Weise
denselben Bildausschnitt niemals mehreren Rezepten zu. Logos, Zutatenfotos, Werbung,
dekorative Elemente und Fotos eines benachbarten Rezeptes sind keine Kandidaten. Lasse die
Liste im Zweifel leer. Die Bounding Box eines Bildkandidaten umfasst ausschließlich das
Gerichtbild, ohne Rezepttext oder benachbarte Fotos.
""".strip()


def _target_language_instruction(locale: Locale) -> str:
    return translate(
        locale,
        "ai.import_language",
        language=LOCALES[locale].ai_language,
    )


def extraction_system_prompt(locale: Locale) -> str:
    return f"{EXTRACTION_SYSTEM_PROMPT}\n\n{_target_language_instruction(locale)}"


def detection_system_prompt(locale: Locale) -> str:
    return f"{DETECTION_SYSTEM_PROMPT}\n\n{_target_language_instruction(locale)}"


def image_match_system_prompt(locale: Locale) -> str:
    return f"{IMAGE_MATCH_SYSTEM_PROMPT}\n\n{_target_language_instruction(locale)}"


IMAGE_MATCH_SYSTEM_PROMPT = """
Du prüfst, ob ein einzelner Bildausschnitt eindeutig als Rezeptbild zu genau einem angegebenen
Rezept passt. Antworte ausschließlich gemäß dem bereitgestellten JSON-Schema. Setze
`matches_recipe` nur dann auf true, wenn der Ausschnitt ein fertiges Gericht zeigt, dessen
sichtbare Merkmale plausibel zu Titel, Beschreibung und Zutaten des angegebenen Rezeptes
passen. Logos, Textseiten, Zutaten, Küchengeräte, Dekoration, mehrere unklare Gerichte oder
ein erkennbar anderes Gericht müssen abgelehnt werden. Entscheide im Zweifel mit false.
""".strip()


def extraction_prompt(existing_category_paths: list[str]) -> str:
    category_hint = "\n".join(f"- {path}" for path in existing_category_paths[:500])
    return (
        "Extrahiere ausschließlich das eine Rezept aus den beigefügten, bereits zugeordneten "
        "Ausschnitten.\n\n"
        "Bereits vorhandene Kategoriepfade (nur verwenden, wenn passend):\n"
        f"{category_hint or '- Noch keine Kategorien vorhanden'}"
    )


def detection_prompt() -> str:
    return (
        "Finde alle eigenständigen Rezepte, ihre vollständigen Quellregionen und nur die "
        "jeweils eindeutig passenden Gerichtbilder im beigefügten Material."
    )


def image_match_prompt(*, title: str, description: str | None, ingredients: list[str]) -> str:
    ingredient_hint = ", ".join(ingredients[:40]) or "keine sicher erkannten Zutaten"
    return (
        f"Rezepttitel: {title}\n"
        f"Beschreibung: {description or 'keine'}\n"
        f"Zutaten: {ingredient_hint}\n\n"
        "Prüfe den beigefügten Ausschnitt ausschließlich gegen dieses Rezept."
    )
