"""Zugriff auf das ACC Shared Memory (nur unter Windows, während ACC läuft).

ACC legt drei Memory-Mapped-Files an: acpmf_physics, acpmf_graphics, acpmf_static.
Die Structs hier sind auf die Felder gekürzt, die wir brauchen (alles davor muss
aber exakt stimmen, damit die Offsets passen).
"""
from __future__ import annotations

import ctypes

import sys
from ctypes import c_float, c_int, c_uint16

F4 = c_float * 4
F3 = c_float * 3


class Physics(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ("packetId", c_int), ("gas", c_float), ("brake", c_float), ("fuel", c_float),
        ("gear", c_int), ("rpms", c_int), ("steerAngle", c_float), ("speedKmh", c_float),
        ("velocity", F3), ("accG", F3),
        ("wheelSlip", F4), ("wheelLoad", F4), ("wheelsPressure", F4),
        ("wheelAngularSpeed", F4), ("tyreWear", F4), ("tyreDirtyLevel", F4),
        ("tyreCoreTemperature", F4), ("camberRAD", F4), ("suspensionTravel", F4),
        ("drs", c_float), ("tc", c_float), ("heading", c_float), ("pitch", c_float),
        ("roll", c_float), ("cgHeight", c_float), ("carDamage", c_float * 5),
        ("numberOfTyresOut", c_int), ("pitLimiterOn", c_int), ("abs", c_float),
        ("kersCharge", c_float), ("kersInput", c_float), ("autoShifterOn", c_int),
        ("rideHeight", c_float * 2), ("turboBoost", c_float), ("ballast", c_float),
        ("airDensity", c_float), ("airTemp", c_float), ("roadTemp", c_float),
        ("localAngularVel", F3), ("finalFF", c_float), ("performanceMeter", c_float),
        ("engineBrake", c_int), ("ersRecoveryLevel", c_int), ("ersPowerLevel", c_int),
        ("ersHeatCharging", c_int), ("ersIsCharging", c_int), ("kersCurrentKJ", c_float),
        ("drsAvailable", c_int), ("drsEnabled", c_int),
        ("brakeTemp", F4), ("clutch", c_float),
        ("tyreTempI", F4), ("tyreTempM", F4), ("tyreTempO", F4),
        ("isAIControlled", c_int),
        ("tyreContactPoint", F3 * 4), ("tyreContactNormal", F3 * 4),
        ("tyreContactHeading", F3 * 4),
        ("brakeBias", c_float), ("localVelocity", F3),
        ("P2PActivations", c_int), ("P2PStatus", c_int), ("currentMaxRpm", c_int),
        ("mz", F4), ("fx", F4), ("fy", F4), ("slipRatio", F4), ("slipAngle", F4),
        ("tcinAction", c_int), ("absInAction", c_int), ("suspensionDamage", F4), ("tyreTemp", F4),
    ]


class Graphics(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ("packetId", c_int), ("status", c_int), ("session", c_int),
        ("currentTime", c_uint16 * 15), ("lastTime", c_uint16 * 15),
        ("bestTime", c_uint16 * 15), ("split", c_uint16 * 15),
        ("completedLaps", c_int), ("position", c_int),
        ("iCurrentTime", c_int), ("iLastTime", c_int), ("iBestTime", c_int),
        ("sessionTimeLeft", c_float), ("distanceTraveled", c_float),
        ("isInPit", c_int), ("currentSectorIndex", c_int), ("lastSectorTime", c_int),
        ("numberOfLaps", c_int), ("tyreCompound", c_uint16 * 33),
        ("replayTimeMultiplier", c_float), ("normalizedCarPosition", c_float),
        ("activeCars", c_int), ("carCoordinates", F3 * 60), ("carID", c_int * 60), ("playerCarID", c_int),
        ("penaltyTime", c_float), ("flag", c_int), ("penalty", c_int), ("idealLineOn", c_int),
        ("isInPitLane", c_int),
    ]


class Static(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ("smVersion", c_uint16 * 15), ("acVersion", c_uint16 * 15),
        ("numberOfSessions", c_int), ("numCars", c_int),
        ("carModel", c_uint16 * 33), ("track", c_uint16 * 33),
    ]


def wstr(arr) -> str:
    """UTF-16 Array (wchar_t unter Windows) -> str."""
    out = []
    for ch in arr:
        if ch == 0:
            break
        out.append(chr(ch))
    return "".join(out)


def check_layout() -> list[str]:
    """Plausibilitätscheck der Offsets gegen die bekannte ACC-Struct-Definition."""
    problems = []
    if Graphics.normalizedCarPosition.offset != 248:
        problems.append(f"Graphics.normalizedCarPosition offset {Graphics.normalizedCarPosition.offset} != 248")
    if Static.carModel.offset != 68 or Static.track.offset != 134:
        problems.append("Static.carModel/track Offsets stimmen nicht")
    if Graphics.isInPitLane.offset != 1236:
        problems.append(f"Graphics.isInPitLane offset {Graphics.isInPitLane.offset} != 1236")
    if Physics.brakeBias.offset != 564:
        problems.append(f"Physics.brakeBias offset {Physics.brakeBias.offset} != 564")
    for st in (Physics, Graphics, Static):
        if ctypes.sizeof(st) > 4096:
            problems.append(f"{st.__name__} größer als eine Speicherseite")
    return problems


class ACCReader:
    def __init__(self):
        if sys.platform != "win32":
            raise RuntimeError("Das Shared Memory von ACC gibt es nur unter Windows (auf dem Rechner, auf dem ACC läuft).")
        self._maps = {}
        self.physics = self._open("acpmf_physics", Physics)
        self.graphics = self._open("acpmf_graphics", Graphics)
        self.static = self._open("acpmf_static", Static)

    def _open(self, name, struct):
        # Nur ÖFFNEN, nie anlegen: mmap(-1, tagname=...) würde die Fläche (zu klein) selbst erzeugen, wenn ACC noch
        # nicht läuft – ACC fände beim Start dann unsere kleine Fläche vor.
        k = _k32()
        size = ctypes.sizeof(struct)
        h = k.OpenFileMappingW(FILE_MAP_READ, False, "Local\\" + name)
        if not h:
            self.close()
            raise RuntimeError(f"Shared Memory '{name}' nicht gefunden – läuft ACC?")
        # Länge 0 = ganze Fläche (mind. eine 4-KB-Seite) -> auch ältere ACC-Versionen mit kürzerem Struct gehen
        ptr = k.MapViewOfFile(h, FILE_MAP_READ, 0, 0, 0)
        if not ptr:
            k.CloseHandle(h)
            self.close()
            raise RuntimeError(f"Shared Memory '{name}' nicht lesbar (Fehler {ctypes.get_last_error()})")
        self._maps[name] = (h, ptr)
        return lambda: struct.from_buffer_copy(ctypes.string_at(ptr, size))

    def read(self):
        return self.physics(), self.graphics(), self.static()

    def close(self):
        k = _k32()
        for h, ptr in self._maps.values():
            k.UnmapViewOfFile(ptr)
            k.CloseHandle(h)
        self._maps = {}


FILE_MAP_READ = 0x0004
_K32 = None


def _k32():
    global _K32
    if _K32 is None:
        from ctypes import wintypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenFileMappingW.restype = wintypes.HANDLE
        k.OpenFileMappingW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        k.MapViewOfFile.restype = ctypes.c_void_p
        k.MapViewOfFile.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_size_t]
        k.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        _K32 = k
    return _K32
