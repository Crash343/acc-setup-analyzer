"""Setup-Wissen für ACC (GT3): Fix-Pakete mit exakten Setup-JSON-Pfaden und Click-Deltas.

Quelle: Setup Ripple von JojoJing (https://github.com/jojojing-dev/setup-ripple, MIT-Lizenz, siehe
vendor/LICENSE-setup-ripple-MIT). Die Pakete fxEntryUnder … fxPowerPush, die Symptom-Ursachen und das
Druckfenster (ca. 26–27 psi heiß, 20,3 psi + 0,1 psi/Click) stammen von dort und wurden ins Deutsche übertragen.
Die Pakete fxFrontLock/fxRearLock sind eigene Kombinationen aus den Ursachen "frontLock" bzw. "brakeBias" dort.

Richtungsannahmen (wie in Setup Ripple): höherer Click-Wert = mehr Bremsbalance nach vorne, härtere Feder/Stabi,
mehr Flügel, höhere Fahrhöhe, mehr Preload, mehr ABS/TC, Bremsbelüftung weiter offen. Im Garage-Screen kurz prüfen.
"""
from __future__ import annotations

PSI_BASE = 20.3        # Reifendruck in psi = 20.3 + 0.1 * Clicks
PSI_PER_CLICK = 0.1

P_BIAS = "advancedSetup.mechanicalBalance.brakeBias"
P_PRELOAD = "advancedSetup.drivetrain.preload"
P_ARB_F = "advancedSetup.mechanicalBalance.aRBFront"
P_ARB_R = "advancedSetup.mechanicalBalance.aRBRear"
P_SPRING = "advancedSetup.mechanicalBalance.wheelRate"
P_WING = "advancedSetup.aeroBalance.rearWing"
P_RIDE = "advancedSetup.aeroBalance.rideHeight"
P_DUCT = "advancedSetup.aeroBalance.brakeDuct"
P_ABS = "basicSetup.electronics.abs"
P_TC = "basicSetup.electronics.tC1"
P_PRESS = "basicSetup.tyres.tyrePressure"
P_CAMBER = "basicSetup.alignment.camber"
P_FUEL = "basicSetup.strategy.fuel"                # Liter
P_FUEL_PER_LAP = "basicSetup.strategy.fuelPerLap"  # Liter/Runde (von ACC geschätzt)

WHEEL_DE = ["VL", "VR", "HL", "HR"]


def _c(p, d, label, why, i=None):
    return dict(p=p, i=i, d=d, label=label, why=why)


FIXES = {
    "fxEntryUnder": dict(
        name="Weniger Untersteuern beim Einlenken", symptom="entryUnder",
        changes=[
            _c(P_BIAS, -2, "Bremsbalance nach hinten (≈ 0,5 %)", "Das wichtigste Werkzeug für Rotation beim Anbremsen"),
            _c(P_PRELOAD, -1, "Diff-Preload weniger", "Weniger Sperre im Schub"),
            _c(P_ARB_F, -1, "Stabi vorne weicher", "Hilft auch in der Kurvenmitte"),
        ]),
    "fxEntrySnap": dict(
        name="Ruhigeres Heck beim Einlenken", symptom="entrySnap",
        changes=[
            _c(P_BIAS, +2, "Bremsbalance nach vorne (≈ 0,5 %)", "Mehr Stabilität beim Bremsen"),
            _c(P_PRELOAD, +1, "Diff-Preload mehr", "Mehr Sperre beruhigt das Heck im Schub"),
        ]),
    "fxMidUnder": dict(
        name="Weniger Untersteuern in der Kurvenmitte", symptom="midUnder",
        changes=[
            _c(P_ARB_F, -1, "Stabi vorne weicher", "Sauberstes Werkzeug für die Kurvenmitte"),
            _c(P_SPRING, -1, "Feder vorne links weicher", "Mehr mechanischer Grip", i=0),
            _c(P_SPRING, -1, "Feder vorne rechts weicher", "Mehr mechanischer Grip", i=1),
        ]),
    "fxMidOver": dict(
        name="Weniger Übersteuern in der Kurvenmitte", symptom="midOver",
        changes=[
            _c(P_ARB_R, -1, "Stabi hinten weicher", "Beruhigt das Heck"),
            _c(P_SPRING, -1, "Feder hinten links weicher", "Beruhigt das Heck", i=2),
            _c(P_SPRING, -1, "Feder hinten rechts weicher", "Beruhigt das Heck", i=3),
        ]),
    "fxFastUnder": dict(
        name="Weniger Untersteuern in schnellen Kurven", symptom="fastUnder",
        changes=[
            _c(P_WING, -1, "Heckflügel 1 Stufe weniger", "Bonus beim Topspeed"),
            _c(P_RIDE, -1, "Fahrhöhe vorne niedriger", "Mehr Rake = mehr Frontabtrieb", i=0),
        ]),
    "fxFastOver": dict(
        name="Mehr Stabilität in schnellen Kurven", symptom="fastOver",
        changes=[
            _c(P_WING, +1, "Heckflügel 1 Stufe mehr", "Größte Einzelmaßnahme"),
            _c(P_RIDE, -1, "Fahrhöhe hinten niedriger", "Etwas weniger Rake", i=2),
        ]),
    "fxTopSpeed": dict(
        name="Mehr Topspeed", symptom=None,
        changes=[_c(P_WING, -1, "Heckflügel 1 Stufe weniger", "Weniger Luftwiderstand, weniger Heckgrip in schnellen Kurven")]),
    "fxTraction": dict(
        name="Mehr Traktion am Kurvenausgang", symptom="exitSpin",
        changes=[
            _c(P_SPRING, -1, "Feder hinten links weicher", "Heck hakt besser ein", i=2),
            _c(P_SPRING, -1, "Feder hinten rechts weicher", "Heck hakt besser ein", i=3),
            _c(P_ARB_R, -1, "Stabi hinten weicher", "Hält das kurveninnere Hinterrad am Boden"),
            _c(P_PRELOAD, +1, "Diff-Preload mehr", "Verhindert Durchdrehen eines einzelnen Rades"),
        ]),
    "fxPowerPush": dict(
        name="Weniger Schieben beim Gasgeben", symptom="exitPush",
        changes=[
            _c(P_PRELOAD, -1, "Diff-Preload weniger", "Ein gesperrtes Diff will geradeaus"),
            _c(P_SPRING, +1, "Feder hinten links härter", "Weniger Einfedern hinten hebt die Nase nicht", i=2),
            _c(P_SPRING, +1, "Feder hinten rechts härter", "Weniger Einfedern hinten hebt die Nase nicht", i=3),
        ]),
    # eigene Kombinationen aus den Ursachen in Setup Ripple
    "fxFrontLock": dict(
        name="Weniger Blockieren der Vorderachse", symptom="frontLock",
        changes=[
            _c(P_BIAS, -2, "Bremsbalance nach hinten (≈ 0,5 %)", "Entlastet die Vorderachse beim Bremsen"),
            _c(P_ABS, +1, "ABS 1 Stufe höher", "Kostet in ACC fast nichts"),
        ]),
    "fxRearLock": dict(
        name="Stabileres Heck beim Bremsen", symptom="entrySnap",
        changes=[_c(P_BIAS, +2, "Bremsbalance nach vorne (≈ 0,5 %)", "Mehr Stabilität beim Bremsen")]),
}

# Weitere Ursachen, die ohne Setup-Änderung (oder mit anderem Hebel) zu prüfen sind
SYMPTOM_CHECKS = {
    "frontLock": ["Blockieren erst spät im Stint → meist Reifenverschleiß, zuerst prüfen.",
                  "Bremsscheiben außerhalb ihres Temperaturfensters → Bremsbelüftung anpassen.",
                  "Zu viel Vorderachs-Sturz vergrößert die Aufstandsfläche beim Geradeausbremsen nicht – weniger Sturz kann helfen."],
    "exitSpin": ["Dreht nur das kurveninnere Rad durch → Diff-Preload; beide Räder → Heckgrip (Federn/Stabi).",
                 "Etwas mehr Spur hinten (toe-in) beruhigt Kurvenausgänge.",
                 "TC um 1 Stufe erhöhen, solange die mechanische Ursache behoben wird. TC maskiert, behebt aber nichts."],
    "exitPush": ["Typisch bei engen Ausgängen mit hohem Preload.",
                 "Zu hohe TC fühlt sich wie Schieben an → 1 Stufe weniger testen."],
    "hotPressures": ["Kalte Drücke zu hoch gesetzt → 0,2–0,5 psi weniger und neu testen.",
                     "Bremsbelüftung 1 Stufe weiter öffnen nimmt Wärme aus den Felgen.",
                     "Zu viel Spur ist eine ständige Wärmequelle.",
                     "Wer gegen das Auto kämpft, kocht die Reifen – erst die Balance beheben."],
    "coldTyres": ["Bremsbelüftung 1 Stufe schließen: Bremswärme wärmt die Reifen.",
                  "Kalte Drücke etwas höher, damit das Fenster früher erreicht wird.",
                  "Etwas mehr Sturz/Spur bringt mehr Arbeit in die Lauffläche."],
    "kerbLaunch": ["Schnelle Dämpfung (Bump/Rebound fast) weicher, damit das Rad den Kerb schluckt.",
                   "Bumpstop-Rate weicher oder mehr Weg.",
                   "Fahrhöhe vorne ein paar Millimeter höher bringt echte Nachgiebigkeit."],
}

TYPICAL = dict(brake_bias_front_pct=(55, 62))  # typischer GT3-Bereich laut Setup Ripple


def describe(fix_id: str) -> list[str]:
    """Textzeilen für einen Fix inkl. Click-Richtung."""
    out = []
    for c in FIXES[fix_id]["changes"]:
        out.append(f"{c['label']} ({c['d']:+d} Click{'s' if abs(c['d']) != 1 else ''}) – {c['why']}")
    return out
