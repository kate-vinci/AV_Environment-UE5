# -*- coding: utf-8 -*-
"""
add_sensor_rigs.py - places the sensors of the ego vehicle in the level, at the parking position of each zone (W, F, T, O).

Each rig is a group of actors in the World Outliner folder Sensors/<zone>:
    Sensors_<zone>_rig          root (a Note actor holding a short description), on the ground, facing the ego heading
    Sensors_<zone>_cam0_left    CameraActor, yaw heading + 90 deg (left)
    Sensors_<zone>_cam1_front   CameraActor, yaw heading
    Sensors_<zone>_cam2_right   CameraActor, yaw heading - 90 deg (right)
    Sensors_<zone>_mic0 ... mic7  TargetPoint actors at the 8 microphones of the array

Cameras: 1224 x 1024 (aspect ratio 1.1953, constrained), horizontal field of view 91.0902 deg, 1.99 m above the ground,
pitch and roll 0, manual exposure with the compensation of the zone and camera (the values used for the dataset renders).
Microphones: 8 omnidirectional capsules, 45 deg apart, mic0 to the front, numbered clockwise seen from above; even
channels on a 28.5 mm radius, odd channels on 21.5 mm; centre 1.90 m above the ground, at the same horizontal position as
the cameras. None of these actors is visible in game views or scene captures.

Run it once, with the level open (Output Log, "Python" or "Cmd" mode):

    py "<path>/add_sensor_rigs.py"              adds the missing rigs and saves the level
    py "<path>/add_sensor_rigs.py" --replace    replaces the rigs made by this script
    py "<path>/add_sensor_rigs.py" --test       temporary rigs (not saved with the level), level not saved

The cameras show the fixed parking views. For a run, render_cameras.py places the same cameras at the
microphone position of every row of geometry.csv.
"""
import math
import sys

import unreal

POSES = {                       # parking position of the ego in each zone: East, North (m), heading (deg, counter-clockwise from East)
    "W": (-87.15, 26.78, 218.04),
    "F": (-17.48, -46.37, -132.0),
    "T": (-110.0, -53.5, 7.0),
    "O": (-152.0, 316.0, 80.0),
}
EXPOSURE = {                    # exposure compensation (EV) per zone and camera
    "W": {"cam0": 2.5, "cam1": 2.0, "cam2": 2.1},
    "F": {"cam0": 2.5, "cam1": 1.6, "cam2": 2.1},
    "T": {"cam0": 2.5, "cam1": 2.0, "cam2": 2.1},
    "O": {"cam0": 1.25, "cam1": 0.85, "cam2": 0.95},
}
CAMERAS = [("cam0", "left", 90.0), ("cam1", "front", 0.0), ("cam2", "right", -90.0)]
IMAGE_W, IMAGE_H, HFOV_DEG = 1224, 1024, 91.0902
CAMERA_HEIGHT_CM, MIC_HEIGHT_CM = 199.0, 190.0
MIC_RADII_M = (0.0285, 0.0215)  # even, odd capsules

_EAS = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
_TRACE = getattr(unreal.TraceTypeQuery, "ECC_VISIBILITY", None) or unreal.TraceTypeQuery.TRACE_TYPE_QUERY1
_UES = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
ARGS = sys.argv[1:]
REPLACE, TEST = "--replace" in ARGS, "--test" in ARGS


def log(msg):
    unreal.log("[add_sensor_rigs] " + msg)


def world():
    return _UES.get_editor_world()


def level_actors():
    return list(_EAS.get_all_level_actors())


def ground_z(x, y):
    """Height of the flat ground (visible static meshes within +-15 cm of z = 0) under (x, y), in cm."""
    ground = set()
    for a in level_actors():
        try:
            if a.get_class().get_name() == "StaticMeshActor" and not a.get_editor_property("hidden"):
                o, e = a.get_actor_bounds(False)
                if o.z - e.z >= -15.0 and o.z + e.z <= 15.0:
                    ground.add(a)
        except Exception:
            pass
    ignore = []
    for _ in range(12):
        hit = unreal.SystemLibrary.line_trace_single(world(), unreal.Vector(x, y, 400.0), unreal.Vector(x, y, -600.0),
                                                     _TRACE, False, ignore, unreal.DrawDebugTrace.NONE,
                                                     True, unreal.LinearColor.RED, unreal.LinearColor.GREEN, 0.0)
        if hit is None:
            return 0.0
        t = hit.to_tuple()
        if not t[0]:
            return 0.0
        if t[9] is None or t[9] in ground:
            return t[4].z
        ignore.append(t[9])
    return 0.0


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


def spawn(cls, loc, yaw, label, folder):
    a = _EAS.spawn_actor_from_class(cls, loc, unreal.Rotator(pitch=0.0, yaw=yaw, roll=0.0), transient=TEST)
    a.set_actor_label(label)
    a.set_folder_path(folder)
    return a


def add_rig(zone):
    E, N, heading = POSES[zone]
    x, y = 100.0 * E, -100.0 * N
    yaw = -heading                                   # Unreal yaw of the ego heading
    z = ground_z(x, y)
    folder = "Sensors/" + zone
    fwd = (math.cos(math.radians(yaw)), math.sin(math.radians(yaw)))
    right = (-math.sin(math.radians(yaw)), math.cos(math.radians(yaw)))

    root_cls = getattr(unreal, "Note", None) or unreal.TargetPoint
    root = spawn(root_cls, unreal.Vector(x, y, z), yaw, "Sensors_%s_rig" % zone, folder)
    try:
        root.set_editor_property("text", "Ego sensors, zone %s: cameras cam0 (left), cam1 (front), cam2 (right), %d x %d, horizontal FOV "
                                         "%.4f deg, %.2f m above the ground; microphones mic0 (front) to mic7, clockwise, %.2f m above the "
                                         "ground." % (zone, IMAGE_W, IMAGE_H, HFOV_DEG, CAMERA_HEIGHT_CM / 100, MIC_HEIGHT_CM / 100))
    except Exception:
        pass
    made = [root]
    for cam, view, dyaw in CAMERAS:
        a = spawn(unreal.CameraActor, unreal.Vector(x, y, z + CAMERA_HEIGHT_CM), yaw - dyaw, "Sensors_%s_%s_%s" % (zone, cam, view), folder)
        c = a.get_component_by_class(unreal.CameraComponent)
        c.set_editor_property("field_of_view", HFOV_DEG)
        c.set_editor_property("aspect_ratio", IMAGE_W / float(IMAGE_H))
        c.set_editor_property("constrain_aspect_ratio", True)
        c.set_editor_property("post_process_settings", manual_exposure(EXPOSURE[zone][cam]))
        c.set_editor_property("post_process_blend_weight", 1.0)
        made.append(a)
    for k in range(8):
        r = 100.0 * MIC_RADII_M[k % 2]
        a_k = math.radians(45.0 * k)                 # clockwise from the front, seen from above
        px = x + r * (math.cos(a_k) * fwd[0] + math.sin(a_k) * right[0])
        py = y + r * (math.cos(a_k) * fwd[1] + math.sin(a_k) * right[1])
        made.append(spawn(unreal.TargetPoint, unreal.Vector(px, py, z + MIC_HEIGHT_CM), yaw, "Sensors_%s_mic%d" % (zone, k), folder))
    for a in made[1:]:
        a.attach_to_actor(root, "", unreal.AttachmentRule.KEEP_WORLD, unreal.AttachmentRule.KEEP_WORLD,
                          unreal.AttachmentRule.KEEP_WORLD, False)
    return made


def main():
    existing = {}
    for a in level_actors():
        try:
            lab = a.get_actor_label()
        except Exception:
            continue
        if lab.startswith("Sensors_") and lab.split("_")[1] in POSES:
            existing.setdefault(lab.split("_")[1], []).append(a)
    added = []
    for zone in POSES:
        if zone in existing:
            if not REPLACE:
                log("zone %s: rig already present (use --replace to rebuild it)" % zone)
                continue
            for a in existing[zone]:
                _EAS.destroy_actor(a)
        made = add_rig(zone)
        added.append(zone)
        log("zone %s: rig added (%d actors)" % (zone, len(made)))
    if added and not TEST:
        try:
            unreal.get_editor_subsystem(unreal.LevelEditorSubsystem).save_current_level()
        except Exception:
            unreal.EditorLevelLibrary.save_current_level()
        log("level saved")
    log("done: %s" % (", ".join(added) if added else "nothing to add"))


main()
