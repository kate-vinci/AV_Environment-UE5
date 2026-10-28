# -*- coding: utf-8 -*-
"""
render_cameras.py - renders the three cameras of dataset runs in the Unreal Engine 5.8 editor.

For every run folder (geometry.csv + scene.json), each row of geometry.csv (20 Hz) gives one frame:
the ego vehicle and the other vehicles are placed in the level, then the left, front and right
cameras are captured with a SceneCapture2D (1224 x 1024, horizontal FOV 91.0902 deg, Final Color LDR,
manual exposure, 3 warm-up captures for Lumen before the saved one). After the last frame, ffmpeg
encodes one H.264 video per camera at 20 fps, one JPEG per second and per camera is kept, the frame
index is written and an overview video (three cameras side by side + stereo sound) is made.

Run it from the editor with the level open (Output Log, "Python" or "Cmd" mode):

    py "<path>/render_cameras.py" "<path>/render_config.json"

Without an argument the script reads render_config.json next to itself. The editor stays usable
while it runs: the work advances one step per editor tick. To stop cleanly between two runs, create
the stop file named in the configuration (default: STOP_RENDER next to the configuration file).
Progress is written to render_status.json next to the configuration file and to the Output Log.

Outputs, per run (in <run>/<output_subdir>/):
    cameras/cam0.mp4, cam1.mp4, cam2.mp4   left, front, right; one video frame per row of geometry.csv
    frames_1fps.tar                         frames_1fps/<run>_cam<c>_<frame>.jpg: frame 20*k + 10 (middle of second k)
                                            of each camera ("frames_1fps_tar": false keeps the folder instead)
    frames_index.csv                        one row per frame: time, ego pose, camera position, files, vehicles
    overview_3cams.mp4                      side by side (half size) + stereo (microphones 6 = left, 2 = right)

Requirements: Python Editor Script Plugin and Editor Scripting Utilities enabled; ffmpeg on the PATH
(or "ffmpeg" in the configuration). Coordinates: geometry.csv gives East / North in metres; the
level uses X = 100 * E, Y = -100 * N (cm), yaw = -heading.
"""
import builtins
import csv
import json
import math
import os
import shutil
import subprocess
import sys
import tarfile
import time

import unreal

VERSION = "1.1"

# ----------------------------------------------------------------------------------------- defaults
DEFAULTS = {
    "runs": [],                         # run folders (absolute, or relative to the configuration file)
    "runs_root": None,                  # optional: every folder below it that holds geometry.csv + scene.json
    "content_root": None,               # folder holding Vehicules/ and Materiaux/; None = found automatically
    "output_subdir": "",                # outputs in <run>/<output_subdir>/ ("" = the run folder itself)
    "image_width": 1224,
    "image_height": 1024,
    "hfov_deg": 91.0902,
    "camera_height_cm": 199.0,          # above the ground, at the microphone position
    "ego_behind_camera_m": 0.60,        # the ego mesh centre is 0.60 m behind the cameras
    "warmup_captures": 3,
    "ticks_after_move": 2,
    "ticks_after_spawn": 10,
    "ticks_after_sky": 30,
    "frame_step": 1,                    # 1 = every row (20 fps)
    "t_min": 0.0,
    "t_max": None,
    "jpeg_quality": 95,
    "crf": 18,
    "keep_frames": False,               # keep every captured JPEG after encoding
    "frames_1fps_tar": True,            # pack frames_1fps/ into frames_1fps.tar (dataset layout)
    "overview": True,
    "overview_gain": 16.0,
    "audio_files": ["audio_8ch.flac", "sim_8ch.flac"],
    "skip_done": True,
    "light_check_retries": 2,           # re-render a run whose images are dark (sky light not recaptured)
    "closed_barrier_splits": ["NEW"],   # runs under a folder with this name: barrier of zone W closed
    "hide_prefixes": ["ego_W_"],
    "ffmpeg": None,
    "stop_file": "STOP_RENDER",
}

# cameras: name, yaw offset from the heading (deg, counter-clockwise), view
CAMERAS = [("cam0", 90.0, "left"), ("cam1", 0.0, "front"), ("cam2", -90.0, "right")]

# per zone: sky dome material and its parameters, SkyLight intensity, exposure compensation per camera
ZONES = {
    "W": {"dome": "MI_ciel_couvert", "params": {"Intensite": 8.0}, "skylight": 0.14, "ev": {"cam0": 2.5, "cam1": 2.0, "cam2": 2.1}},
    "F": {"dome": "MI_ciel_couvert", "params": {"Intensite": 8.0}, "skylight": 0.14, "ev": {"cam0": 2.5, "cam1": 1.6, "cam2": 2.1}},
    "T": {"dome": "MI_ciel_couvert", "params": {"Intensite": 8.0}, "skylight": 0.14, "ev": {"cam0": 2.5, "cam1": 2.0, "cam2": 2.1}},
    "O": {"dome": "MI_ciel_couvert3", "params": {"K": 4.8, "Intensite": 0.43}, "skylight": 0.066, "ev": {"cam0": 1.25, "cam1": 0.85, "cam2": 0.95}},
}

# vehicle meshes (relative to content_root) and scales (X, Y, Z)
MESHES = {
    "ego": ("Vehicules/SM_car_ego", (1.2, 1.02, 1.157)),
    "BMW": ("Vehicules/SM_car_BMW", (1.195, 1.017, 1.165)),
    "TC": ("Vehicules/SM_car_TC", (1.08, 1.08, 1.08)),
    "T5": ("Vehicules/SM_van_T5", (1.0, 0.9, 0.95)),
    "IDBuzz": ("Vehicules/SM_van_IDBuzz", (1.0, 0.94, 0.95)),
    "moto": ("Vehicules/SM_moto_v2", (1.0, 1.0, 1.0)),
}
MODEL_TO_MESH = {"bmw": "BMW", "dc": "BMW", "tc": "TC", "t5": "T5", "idbuzz": "IDBuzz", "moto": "moto"}
CLASS_TO_MESH = {"car": "TC", "van": "T5", "moto": "moto"}

BARRIER_OPEN, BARRIER_CLOSED = "W_barriere_levee", "W_barriere_fermee"
LABEL_PREFIX = "RENDER_"

_EAS = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
_TRACE = getattr(unreal.TraceTypeQuery, "ECC_VISIBILITY", None) or unreal.TraceTypeQuery.TRACE_TYPE_QUERY1
_UES = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)


# ----------------------------------------------------------------------------------------- configuration
def _script_dir():
    f = globals().get("__file__") or (sys.argv[0] if sys.argv and sys.argv[0].endswith(".py") else "")
    return os.path.dirname(os.path.abspath(f)) if f else os.getcwd()


def load_config():
    args = [a for a in sys.argv[1:] if a.endswith(".json")]
    path = os.path.abspath(args[0]) if args else os.path.join(_script_dir(), "render_config.json")
    cfg = dict(DEFAULTS)
    with open(path, encoding="utf-8") as f:
        cfg.update(json.load(f))
    base = os.path.dirname(path)
    runs = [r if os.path.isabs(r) else os.path.normpath(os.path.join(base, r)) for r in cfg["runs"]]
    if cfg.get("runs_root"):
        root = cfg["runs_root"] if os.path.isabs(cfg["runs_root"]) else os.path.join(base, cfg["runs_root"])
        for d, _, files in sorted(os.walk(root)):
            if "geometry.csv" in files and "scene.json" in files:
                runs.append(os.path.normpath(d))
    cfg["runs"] = runs
    cfg["_dir"] = base
    cfg["stop_file"] = cfg["stop_file"] if os.path.isabs(cfg["stop_file"]) else os.path.join(base, cfg["stop_file"])
    cfg["_status"] = os.path.join(base, "render_status.json")
    return cfg


def log(msg):
    unreal.log("[render_cameras] " + msg)


def find_content_root():
    """The content folder that holds Vehicules/SM_car_ego (the environment's own meshes and materials)."""
    try:
        folders = [str(x) for x in unreal.AssetRegistryHelpers.get_asset_registry().get_sub_paths("/Game", False)]
    except Exception:
        folders = [str(x).rstrip("/") for x in unreal.EditorAssetLibrary.list_assets("/Game", recursive=False, include_folder=True)]
    for p in sorted(folders):
        if unreal.EditorAssetLibrary.does_asset_exist(p + "/Vehicules/SM_car_ego"):
            return p
    raise RuntimeError("no folder of /Game holds Vehicules/SM_car_ego (made by build_vehicles.py): set content_root in the configuration")


def find_ffmpeg(cfg):
    for p in (cfg.get("ffmpeg"), shutil.which("ffmpeg"), "/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg"):
        if p and os.path.exists(p):
            try:
                if subprocess.run([p, "-version"], capture_output=True, timeout=20).returncode == 0:
                    return p
            except Exception:
                pass
    return None


# ----------------------------------------------------------------------------------------- level helpers
def world():
    return _UES.get_editor_world()


def level_actors(prefix=None):
    out = []
    for a in _EAS.get_all_level_actors():
        try:
            if prefix is None or a.get_actor_label().startswith(prefix):
                out.append(a)
        except Exception:
            pass
    return out


def ground_actor_labels():
    """Labels of the flat ground actors: visible static mesh actors whose bounds lie within +-15 cm of z = 0
    (ground, roads, slabs). Walls, fences, trees and grass tufts are not ground."""
    out = set()
    for a in level_actors():
        try:
            if a.get_class().get_name() != "StaticMeshActor" or a.get_editor_property("hidden"):
                continue
            o, e = a.get_actor_bounds(False)
            if o.z - e.z >= -15.0 and o.z + e.z <= 15.0:
                out.add(a.get_actor_label())
        except Exception:
            pass
    return out


class Ground:
    """Ground height under a point: vertical trace from z = 400 cm that skips every actor that is not flat ground."""

    def __init__(self):
        self.labels = ground_actor_labels()
        self.not_ground = []
        self.ignore = []
        self.last_z = 0.0

    def z(self, x, y):
        ignore = list(self.ignore) + self.not_ground
        for _ in range(12):
            hit = None
            for _try in range(3):
                hit = unreal.SystemLibrary.line_trace_single(
                    world(), unreal.Vector(x, y, 400.0), unreal.Vector(x, y, -600.0), _TRACE,
                    False, ignore, unreal.DrawDebugTrace.NONE, True, unreal.LinearColor.RED, unreal.LinearColor.GREEN, 0.0)
                if hit is not None:
                    break
            if hit is None:
                return self.last_z
            t = hit.to_tuple()
            if not t[0]:
                return self.last_z
            actor = t[9]
            if actor is None or actor.get_actor_label() in self.labels:
                self.last_z = t[4].z
                return self.last_z
            self.not_ground.append(actor)
            ignore.append(actor)
        return self.last_z


def apply_sky(cfg, zone):
    """Sky dome material and SkyLight intensity of the zone; the SkyLight is recaptured (effective a few ticks later)."""
    z = ZONES[zone]
    mel = unreal.MaterialEditingLibrary
    mi = unreal.load_asset("%s/Materiaux/%s" % (cfg["content_root"], z["dome"]))
    if mi is None:
        log("sky material not found: %s/Materiaux/%s" % (cfg["content_root"], z["dome"]))
        return
    for k, v in z["params"].items():
        mel.set_material_instance_scalar_parameter_value(mi, k, v)
    mel.update_material_instance(mi)
    for a in level_actors("SM_SkySphere"):
        a.get_component_by_class(unreal.StaticMeshComponent).set_material(0, mi)
    for a in level_actors():
        if a.get_class().get_name() == "SkyLight":
            sky = a.get_component_by_class(unreal.SkyLightComponent)
            sky.set_editor_property("intensity", z["skylight"])
            sky.recapture_sky()


class HiddenState:
    """Hides actors in the renders (Hidden in Game) and restores their previous state at the end."""

    def __init__(self):
        self.saved = {}

    def set(self, actor, hidden):
        if actor not in self.saved:
            try:
                self.saved[actor] = actor.get_editor_property("hidden")
            except Exception:
                return
        actor.set_actor_hidden_in_game(hidden)

    def restore(self):
        for a, h in self.saved.items():
            try:
                a.set_actor_hidden_in_game(h)
            except Exception:
                pass
        self.saved = {}


def manual_exposure(ev):
    pp = unreal.PostProcessSettings()
    pp.set_editor_property("override_auto_exposure_method", True)
    pp.set_editor_property("auto_exposure_method", unreal.AutoExposureMethod.AEM_MANUAL)
    pp.set_editor_property("override_auto_exposure_bias", True)
    pp.set_editor_property("auto_exposure_bias", float(ev))
    try:
        pp.set_editor_property("override_auto_exposure_apply_physical_camera_exposure", True)
        pp.set_editor_property("auto_exposure_apply_physical_camera_exposure", False)
    except Exception:
        pass
    return pp


class Capture:
    """One transient SceneCapture2D reused for every image (Final Color LDR into an RGBA8 render target)."""

    def __init__(self, cfg):
        self.cfg = cfg
        for a in level_actors(LABEL_PREFIX + "capture"):
            _EAS.destroy_actor(a)
        self.actor = _EAS.spawn_actor_from_class(unreal.SceneCapture2D, unreal.Vector(0, 0, 0), unreal.Rotator(pitch=0, yaw=0, roll=0), transient=True)
        self.actor.set_actor_label(LABEL_PREFIX + "capture")
        self.comp = self.actor.get_component_by_class(unreal.SceneCaptureComponent2D)
        self.rt = unreal.RenderingLibrary.create_render_target2d(world(), int(cfg["image_width"]), int(cfg["image_height"]),
                                                                  unreal.TextureRenderTargetFormat.RTF_RGBA8)
        self.comp.set_editor_property("texture_target", self.rt)
        self.comp.set_editor_property("capture_source", unreal.SceneCaptureSource.SCS_FINAL_COLOR_LDR)
        self.comp.set_editor_property("fov_angle", float(cfg["hfov_deg"]))
        self.comp.set_editor_property("capture_every_frame", False)
        self.comp.set_editor_property("capture_on_movement", False)
        try:
            self.comp.set_editor_property("always_persist_rendering_state", True)
        except Exception:
            pass
        self.options = unreal.ImageWriteOptions()
        self.options.set_editor_property("format", unreal.DesiredImageFormat.JPG)
        self.options.set_editor_property("compression_quality", int(cfg["jpeg_quality"]))
        for k, v in (("async", True), ("overwrite_file", True)):
            try:
                self.options.set_editor_property(k, v)
            except Exception:
                pass

    def shoot(self, location, rotation, ev, path):
        """warm-up captures + 1, then an asynchronous JPEG export of the render target."""
        self.actor.set_actor_location_and_rotation(location, rotation, False, True)
        self.comp.set_editor_property("post_process_settings", manual_exposure(ev))
        self.comp.set_editor_property("post_process_blend_weight", 1.0)
        for _ in range(int(self.cfg["warmup_captures"]) + 1):
            self.comp.capture_scene()
        unreal.ImageWriteBlueprintLibrary.export_to_disk(self.rt, path, self.options)

    def destroy(self):
        try:
            _EAS.destroy_actor(self.actor)
        except Exception:
            pass


def jpeg_complete(p):
    try:
        with open(p, "rb") as f:
            if f.read(2) != b"\xff\xd8":
                return False
            f.seek(0, 2)
            n = f.tell()
            f.seek(max(0, n - 65536))
            return b"\xff\xd9" in f.read()
    except Exception:
        return False


# ----------------------------------------------------------------------------------------- one run
class Run:
    def __init__(self, cfg, path, ground):
        self.cfg = cfg
        self.path = path
        self.name = os.path.basename(os.path.normpath(path))
        self.out = os.path.join(path, cfg["output_subdir"]) if cfg["output_subdir"] else path
        self.frames_dir = os.path.join(self.out, "_frames")
        self.scene = json.load(open(os.path.join(path, "scene.json"), encoding="utf-8"))
        self.zone = self.scene.get("zone") or self.scene.get("spec", {}).get("zone") or "O"
        if self.zone not in ZONES:
            raise ValueError("unknown zone %r" % self.zone)
        with open(os.path.join(path, "geometry.csv"), encoding="utf-8") as f:
            self.rows = list(csv.DictReader(line for line in f if not line.startswith("#")))
        seen = {}
        self.vehicles = []
        for v in self.scene.get("vehicules", []):           # a repeated name is <name>2 in geometry.csv (tc, tc2)
            k = seen.get(v["nom"], 0) + 1
            seen[v["nom"]] = k
            if k > 1:
                v = dict(v, nom="%s%d" % (v["nom"], k))
            if "%s_E_m" % v["nom"] in self.rows[0]:
                self.vehicles.append(v)
        t_max = cfg["t_max"]
        self.frames = [i for i in range(0, len(self.rows), max(1, int(cfg["frame_step"])))
                       if float(self.rows[i]["time_s"]) >= float(cfg["t_min"]) and (t_max is None or float(self.rows[i]["time_s"]) <= float(t_max))]
        self.ground = ground
        self.ego = None
        self.actors = {}
        self.index = []
        self.info = {"run": self.name, "zone": self.zone, "frames": len(self.frames)}
        self.dark = False

    def done(self):
        cams = os.path.join(self.out, "cameras")
        return all(os.path.exists(os.path.join(cams, c + ".mp4")) for c, _, _ in CAMERAS) and os.path.exists(os.path.join(self.out, "frames_index.csv"))

    def mesh_key(self, v):
        for k in (v.get("modele"), v.get("nom"), (v.get("nom") or "").rstrip("0123456789")):
            if k and k in MODEL_TO_MESH:
                return MODEL_TO_MESH[k]
        return CLASS_TO_MESH.get(v.get("classe", ""))

    def spawn_mesh(self, key, label):
        rel, scale = MESHES[key]
        sm = unreal.EditorAssetLibrary.load_asset("%s/%s" % (self.cfg["content_root"], rel))
        if sm is None:
            raise RuntimeError("mesh not found: %s/%s" % (self.cfg["content_root"], rel))
        a = _EAS.spawn_actor_from_object(sm, unreal.Vector(0, 0, -10000), unreal.Rotator(pitch=0, yaw=0, roll=0), transient=True)
        a.set_actor_scale3d(unreal.Vector(*scale))
        a.set_actor_label(LABEL_PREFIX + label)
        return a

    def spawn(self):
        for a in level_actors(LABEL_PREFIX):
            if a.get_actor_label() != LABEL_PREFIX + "capture":
                _EAS.destroy_actor(a)
        self.ego = self.spawn_mesh("ego", "ego")
        for v in self.vehicles:
            key = self.mesh_key(v)
            if key:
                self.actors[v["nom"]] = self.spawn_mesh(key, v["nom"])
        self.ground.ignore = [self.ego] + list(self.actors.values())

    def destroy(self):
        for a in [self.ego] + list(self.actors.values()):
            try:
                if a:
                    _EAS.destroy_actor(a)
            except Exception:
                pass

    def place(self, i):
        r = self.rows[i]
        cap = float(r["ego_heading_deg"])
        mx, my = 100.0 * float(r["mic_E_m"]), -100.0 * float(r["mic_N_m"])
        mz = self.ground.z(mx, my)
        back = 100.0 * float(self.cfg["ego_behind_camera_m"])
        ex, ey = mx - back * math.cos(math.radians(cap)), my + back * math.sin(math.radians(cap))
        self.ego.set_actor_location_and_rotation(unreal.Vector(ex, ey, self.ground.z(ex, ey) + 1.0), unreal.Rotator(pitch=0, yaw=-cap, roll=0), False, True)
        for name, a in self.actors.items():
            vx, vy = 100.0 * float(r[name + "_E_m"]), -100.0 * float(r[name + "_N_m"])
            a.set_actor_location_and_rotation(unreal.Vector(vx, vy, self.ground.z(vx, vy) + 1.0),
                                              unreal.Rotator(pitch=0, yaw=-float(r[name + "_heading_deg"]), roll=0), False, True)
        return mx, my, mz, cap

    def frame_file(self, cam, i):
        return os.path.join(self.frames_dir, cam, "%s_%s_%05d.jpg" % (self.name, cam, i))

    def capture_steps(self, capture):
        """Capture of every frame, advanced one step per editor tick."""
        cfg = self.cfg
        for cam, _, _ in CAMERAS:
            os.makedirs(os.path.join(self.frames_dir, cam), exist_ok=True)
        t0 = time.time()
        for n, i in enumerate(self.frames):
            mx, my, mz, cap = self.place(i)
            for _ in range(int(cfg["ticks_after_move"])):
                yield
            r = self.rows[i]
            row = {"frame": i, "time_s": r["time_s"], "zone": self.zone, "ego_E_m": r["ego_E_m"], "ego_N_m": r["ego_N_m"],
                   "ego_heading_deg": r["ego_heading_deg"], "cam_x_cm": round(mx, 1), "cam_y_cm": round(my, 1),
                   "cam_z_cm": round(mz + float(cfg["camera_height_cm"]), 1)}
            for cam, dyaw, _ in CAMERAS:
                rot = unreal.Rotator(pitch=0.0, yaw=-(cap + dyaw), roll=0.0)
                loc = unreal.Vector(mx, my, mz + float(cfg["camera_height_cm"]))
                capture.shoot(loc, rot, ZONES[self.zone]["ev"][cam], self.frame_file(cam, i))
                yield
            for v in self.vehicles:
                for k in ("E_m", "N_m", "heading_deg", "speed_kmh"):
                    row["%s_%s" % (v["nom"], k)] = r.get("%s_%s" % (v["nom"], k), "")
            self.index.append(row)
            if n % 20 == 0:
                write_status(cfg, {"run": self.name, "frame": n, "frames": len(self.frames), "elapsed_s": round(time.time() - t0)})
        self.info["capture_s"] = round(time.time() - t0, 1)

    def wait_exports(self, max_s=300.0):
        """Waits (one step per editor tick) until every expected JPEG exists, is complete and keeps its size over two checks."""
        expected = [self.frame_file(c, i) for c, _, _ in CAMERAS for i in self.frames]
        t0 = time.time()
        prev, stable, checked = None, 0, {}

        def complete(p, size):
            if checked.get(p) != size:
                if not jpeg_complete(p):
                    return False
                checked[p] = size
            return True

        while time.time() - t0 < max_s:
            for _ in range(20):
                yield
            sizes = tuple(os.path.getsize(p) if os.path.exists(p) else -1 for p in expected)
            if -1 not in sizes and sizes == prev and all(complete(p, s) for p, s in zip(expected, sizes)):
                stable += 1
                if stable >= 2:
                    break
            else:
                stable = 0
            prev = sizes
            t1 = time.time()
            while time.time() - t1 < 1.5:
                yield
        self.info["export_wait_s"] = round(time.time() - t0, 1)

    def encode(self, ffmpeg):
        """One mp4 per camera (a missing or broken image is replaced by the previous valid one, so that the video keeps exactly
        one frame per index row), one JPEG per second and per camera, then the frame index."""
        cfg = self.cfg
        fps = 20.0 / max(1, int(cfg["frame_step"]))
        cams_dir = os.path.join(self.out, "cameras")
        jpg_dir = os.path.join(self.out, "frames_1fps")
        os.makedirs(cams_dir, exist_ok=True)
        os.makedirs(jpg_dir, exist_ok=True)
        info = {"videos": {}, "replaced": {}, "jpeg_1fps": 0}
        kept = {}
        for cam, _, _ in CAMERAS:
            files, replaced, last = [], [], None
            for i in self.frames:
                p = self.frame_file(cam, i)
                if jpeg_complete(p):
                    last = p
                elif last is not None:
                    replaced.append(i)
                    p = last
                else:
                    replaced.append(i)
                    continue
                files.append(p)
            info["replaced"][cam] = replaced
            if not files:
                continue
            lst = os.path.join(self.frames_dir, cam + ".txt")
            with open(lst, "w") as f:
                for p in files:
                    f.write("file '%s'\nduration %.6f\n" % (p.replace("'", "'\\''"), 1.0 / fps))
                f.write("file '%s'\n" % files[-1].replace("'", "'\\''"))
            mp4 = os.path.join(cams_dir, cam + ".mp4")
            r = subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", lst,
                                "-vf", "fps=%g,format=yuv420p" % fps, "-frames:v", str(len(files)),
                                "-c:v", "libx264", "-crf", str(int(cfg["crf"])), "-movflags", "+faststart", mp4],
                               capture_output=True, text=True)
            info["videos"][cam] = r.returncode == 0 and os.path.exists(mp4) and os.path.getsize(mp4) > 0
            kept[cam] = {}
            for i in self.frames:
                if (i - 10) % 20 == 0 and jpeg_complete(self.frame_file(cam, i)):
                    dst = os.path.join(jpg_dir, os.path.basename(self.frame_file(cam, i)))
                    shutil.copy2(self.frame_file(cam, i), dst)
                    kept[cam][i] = "frames_1fps/" + os.path.basename(dst)
                    info["jpeg_1fps"] += 1
        # frame index
        names = list(self.index[0].keys()) if self.index else []
        head = [k for k in names if not k.startswith(tuple(v["nom"] + "_" for v in self.vehicles))]
        tail = [k for k in names if k not in head]
        cols = head + ["%s_%s" % (c, k) for c, _, _ in CAMERAS for k in ("frame", "jpeg")] + tail
        with open(os.path.join(self.out, "frames_index.csv"), "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            for n, row in enumerate(self.index):
                o = dict(row)
                for c, _, _ in CAMERAS:
                    o[c + "_frame"] = n
                    o[c + "_jpeg"] = kept.get(c, {}).get(row["frame"], "")
                w.writerow(o)
        self.dark = looks_dark(self, ffmpeg)            # checked on the 1 fps images, before they are packed
        if cfg["frames_1fps_tar"] and os.listdir(jpg_dir):
            with tarfile.open(os.path.join(self.out, "frames_1fps.tar"), "w") as tar:
                for f in sorted(os.listdir(jpg_dir)):
                    tar.add(os.path.join(jpg_dir, f), arcname="frames_1fps/" + f)
            shutil.rmtree(jpg_dir, ignore_errors=True)
        if all(info["videos"].get(c) for c, _, _ in CAMERAS) and not cfg["keep_frames"]:
            shutil.rmtree(self.frames_dir, ignore_errors=True)
        self.info["encoding"] = info
        return info

    def overview(self, ffmpeg):
        """cam0 | cam1 | cam2 at half size + stereo sound (microphone 6 = left, 2 = right), the sound starting at the time of frame 0."""
        audio = next((os.path.join(self.path, a) for a in self.cfg["audio_files"] if os.path.exists(os.path.join(self.path, a))), None)
        cams = [os.path.join(self.out, "cameras", c + ".mp4") for c, _, _ in CAMERAS]
        if not all(os.path.exists(c) for c in cams):
            return "cameras missing"
        out = os.path.join(self.out, "overview_3cams.mp4")
        cmd = [ffmpeg, "-y", "-loglevel", "error"]
        for c in cams:
            cmd += ["-i", c]
        flt = "[0:v]scale=iw/2:ih/2[a];[1:v]scale=iw/2:ih/2[b];[2:v]scale=iw/2:ih/2[c];[a][b][c]hstack=inputs=3[v]"
        if audio:
            t0 = float(self.rows[self.frames[0]]["time_s"])
            cmd += ["-ss", "%.3f" % t0, "-i", audio]
            flt += ";[3:a]pan=stereo|c0=c6|c1=c2,volume=%g,alimiter=limit=0.95[s]" % float(self.cfg["overview_gain"])
            cmd += ["-filter_complex", flt, "-map", "[v]", "-map", "[s]", "-c:a", "aac", "-b:a", "192k", "-shortest"]
        else:
            cmd += ["-filter_complex", flt, "-map", "[v]"]
        cmd += ["-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p", "-movflags", "+faststart", out]
        r = subprocess.run(cmd, capture_output=True, text=True)
        return "ok" if r.returncode == 0 else (r.stderr or "")[-300:]


def mean_luma(ffmpeg, image, top):
    crop = "crop=iw:ih*0.35:0:0" if top else "crop=iw:ih*0.45:0:ih*0.55"
    r = subprocess.run([ffmpeg, "-nostdin", "-loglevel", "error", "-i", image, "-vf", crop + ",scale=1:1:flags=area,format=gray",
                        "-f", "rawvideo", "-"], stdin=subprocess.DEVNULL, capture_output=True)
    return r.stdout[0] if r.returncode == 0 and r.stdout else None


def looks_dark(run, ffmpeg):
    """True when, at two of three instants, all three cameras show a dark ground (< 22) under a bright sky (> 150):
    the sky light was not recaptured after a zone change."""
    d = os.path.join(run.out, "frames_1fps")
    dark = 0
    for q in (0.1, 0.5, 0.9):
        flags = []
        for c, _, _ in CAMERAS:
            fs = sorted(f for f in os.listdir(d) if "_%s_" % c in f) if os.path.isdir(d) else []
            if not fs:
                return False
            im = os.path.join(d, fs[min(len(fs) - 1, int(q * len(fs)))])
            g, s = mean_luma(ffmpeg, im, False), mean_luma(ffmpeg, im, True)
            flags.append(g is not None and s is not None and g < 22 and s > 150)
        dark += all(flags)
    return dark >= 2


def write_status(cfg, d):
    try:
        d = dict(d, time=time.strftime("%Y-%m-%d %H:%M:%S"), version=VERSION)
        json.dump(d, open(cfg["_status"], "w", encoding="utf-8"), indent=1)
    except Exception:
        pass


# ----------------------------------------------------------------------------------------- batch
def batch(cfg):
    ffmpeg = find_ffmpeg(cfg)
    if not ffmpeg:
        log("ffmpeg not found: images are captured but not encoded")
    hidden = HiddenState()
    for a in level_actors():
        try:
            if a.get_actor_label().startswith(tuple(cfg["hide_prefixes"])):
                hidden.set(a, True)
        except Exception:
            pass
    ground = Ground()
    capture = Capture(cfg)
    summary = {"runs": len(cfg["runs"]), "done": 0, "skipped": 0, "failed": [], "start": time.strftime("%Y-%m-%d %H:%M:%S"), "results": []}
    current_zone = None
    t_batch = time.time()
    try:
        for k, path in enumerate(cfg["runs"]):
            if os.path.exists(cfg["stop_file"]):
                summary["stopped"] = "stop file before " + os.path.basename(path)
                break
            try:
                run = Run(cfg, path, ground)
            except Exception as ex:
                summary["failed"].append([path, str(ex)[:200]])
                continue
            if cfg["skip_done"] and run.done():
                summary["skipped"] += 1
                continue
            closed = any(part in cfg["closed_barrier_splits"] for part in os.path.normpath(path).split(os.sep))
            for a in level_actors(BARRIER_CLOSED):
                hidden.set(a, not closed)
            for a in level_actors(BARRIER_OPEN):
                hidden.set(a, closed)
            run.info["barrier_W"] = "closed" if closed else "open"
            for attempt in range(1 + int(cfg["light_check_retries"])):
                if run.zone != current_zone or attempt > 0:
                    apply_sky(cfg, run.zone)
                    # the first sky of a session takes longer to be captured (shaders, Lumen surface cache)
                    wait = int(cfg["ticks_after_sky"]) * (4 if current_zone is None else 1 + 3 * attempt)
                    current_zone = run.zone
                    for _ in range(wait):
                        yield
                log("%d/%d %s (zone %s, %d frames, %d vehicles)" % (k + 1, len(cfg["runs"]), run.name, run.zone, len(run.frames), len(run.vehicles)))
                write_status(cfg, {"run": run.name, "frame": 0, "frames": len(run.frames), "summary": summary})
                dark = False
                try:
                    run.spawn()
                    for _ in range(int(cfg["ticks_after_spawn"])):
                        yield
                    for _ in run.capture_steps(capture):
                        yield
                    if ffmpeg:
                        for _ in run.wait_exports():
                            yield
                        run.encode(ffmpeg)
                        dark = run.dark
                        if not dark and cfg["overview"]:
                            run.info["overview"] = run.overview(ffmpeg)
                except Exception as ex:
                    import traceback
                    summary["failed"].append([run.name, traceback.format_exc()[-600:]])
                    log("FAILED %s: %s" % (run.name, ex))
                finally:
                    run.destroy()
                if not dark:
                    summary["results"].append(run.info)
                    summary["done"] += 1
                    break
                log("%s: dark images (sky light), rendering again" % run.name)
                run = Run(cfg, path, ground)
                run.info["barrier_W"] = "closed" if closed else "open"
            else:
                summary["failed"].append([run.name, "dark images after %d attempts" % (1 + int(cfg["light_check_retries"]))])
    finally:
        capture.destroy()
        hidden.restore()
        summary["end"] = time.strftime("%Y-%m-%d %H:%M:%S")
        summary["duration_s"] = round(time.time() - t_batch)
        write_status(cfg, {"finished": True, "summary": summary})
        log("finished: %d rendered, %d skipped, %d failed in %.0f min" % (summary["done"], summary["skipped"], len(summary["failed"]),
                                                                         (time.time() - t_batch) / 60.0))


def _start():
    old = getattr(builtins, "_render_cameras_tick", None)
    if old is not None:
        try:
            unreal.unregister_slate_post_tick_callback(old)
        except Exception:
            pass
    cfg = load_config()
    if not cfg.get("content_root"):
        cfg["content_root"] = find_content_root()
    if os.path.exists(cfg["stop_file"]):
        os.remove(cfg["stop_file"])
    gen = batch(cfg)
    state = {"handle": None, "busy": False}

    def tick(_dt):
        if state["busy"]:
            return
        state["busy"] = True
        try:
            next(gen)
        except StopIteration:
            unreal.unregister_slate_post_tick_callback(state["handle"])
            builtins._render_cameras_tick = None
        except Exception as ex:
            unreal.unregister_slate_post_tick_callback(state["handle"])
            builtins._render_cameras_tick = None
            log("stopped on error: %s" % ex)
        finally:
            state["busy"] = False

    state["handle"] = unreal.register_slate_post_tick_callback(tick)
    builtins._render_cameras_tick = state["handle"]
    log("started: %d run(s), content in %s, status in %s" % (len(cfg["runs"]), cfg["content_root"], cfg["_status"]))


_start()
