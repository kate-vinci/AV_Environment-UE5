# -*- coding: utf-8 -*-
"""
build_vehicles.py - builds the vehicles of the environment (meshes, textures, materials) from three free Fab items.

The vehicle assets of the dataset renders are derived from Fab items that cannot be redistributed. This script rebuilds
them, with the same names and the same content, in <content_root>/Vehicules:

    SM_car_BMW, SM_car_TC, SM_car_ego     copies of SM_Hatchback with a repainted body (black, white, dark blue)
    SM_van_livraison                      the delivery van at real size (4.58 m), front = +X, pivot on the ground
    SM_van_T5, SM_van_IDBuzz              copies of SM_van_livraison with a repainted body (grey; white over blue-green)
    SM_moto_v2                            the motorcycle upright, front = +X, scale 0.30, pivot on the ground
    Textures/, Materiaux/, van_livraison/ the repainted textures, their materials and the imported van

Fab items (all free):
    Vehicle Variety Pack (Switchboard Studios)          added to the project: /Game/VehicleVarietyPack
    Motorcycle Photoscan LOWPOLY                        added to the project: /Game/Fab/Motorcycle_Photoscan_LOWPOLY
    Standard Delivery Van (Slowpoly), OBJ format        files 003.obj and Base_Texture.png in SourceArt/Standard_Delivery_Van

Run it once from the editor (Output Log, "Python" or "Cmd" mode), with any level open:

    py "<path>/build_vehicles.py"
    py "<path>/build_vehicles.py" --content-root /Game/SiteSim --van-dir "<folder with 003.obj and Base_Texture.png>"

Existing assets are kept (an asset that is already present is not rebuilt). The repainting runs in pure Python over a few
minutes; the editor stays usable and the progress is written to the Output Log. Temporary files go to Saved/BuildVehicles.
"""
import builtins
import math
import os
import struct
import sys
import time
import zlib

import unreal

ARGS = sys.argv[1:]


def _arg(name, default):
    return ARGS[ARGS.index(name) + 1] if name in ARGS and ARGS.index(name) + 1 < len(ARGS) else default


PROJECT = unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_dir())
ROOT = _arg("--content-root", "/Game/SiteSim").rstrip("/")
OUT = ROOT + "/Vehicules"
VAN_DIR = _arg("--van-dir", os.path.join(PROJECT, "SourceArt", "Standard_Delivery_Van"))
TMP = os.path.join(unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_saved_dir()), "BuildVehicles")

VVP = "/Game/VehicleVarietyPack"
HATCHBACK = VVP + "/Meshes/SM_Hatchback"
HATCH_TEX = VVP + "/Textures/Hatchback/TX_Hatchback_"
MOTO_SRC = "/Game/Fab/Motorcycle_Photoscan_LOWPOLY/motorcycle_photoscan_lowpoly/StaticMeshes/motorcycle_photoscan_lowpoly"

CARS = {"SM_car_BMW": ("noir", "M_car_noir"), "SM_car_TC": ("blanc", "M_car_blanc2"), "SM_car_ego": ("bleunuit", "M_car_bleunuit")}
CAR_PAINTS = {"noir": (0.012, 0.012, 0.012), "bleunuit": (0.0065, 0.011, 0.032), "blanc": (0.80, 0.80, 0.80)}
VAN_SCALE, VAN_YAW, VAN_ROLL = 2.40, 180.0, 90.0
VAN_WHEELS = ("Circle001", "Circle002", "Object014", "Object019")
VAN_BODY, VAN_BELT = "s_024", 6.5            # body group of 003.obj; belt line of the ID Buzz in OBJ units (about 1.05 m)
T5_GREY, T5_NORM = 0.22, 0.8386
IDB_LOW, IDB_HIGH, IDB_NORM = (0.02, 0.30, 0.40), 0.84, 0.84
MOTO_SCALE = 0.30
MOTO_ROT = (13.914160874945734, -109.23894059241945, -5.9178509036401)        # pitch, yaw, roll
MOTO_M = ((-0.319839668846088, 0.9472904553965114, -0.01853049775627303),
          (-0.916448444014336, -0.3043441552338256, 0.2598016255505562),
          (0.24046795149484498, 0.10007711171731279, 0.9654841977029939))
MOTO_CENTROID_CM = (-4209.253612231612, -231.6495783994796, 181.1566363044892)
MOTO_FOOTPRINT_M = (0.4230094096335981, -0.22341367651956545, 0.03156574877641227)
MOTO_ZMIN_M = -1.912143793947233

EAL = unreal.EditorAssetLibrary
AT = unreal.AssetToolsHelpers.get_asset_tools()
MEL = unreal.MaterialEditingLibrary
EAS = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
SMES = unreal.get_editor_subsystem(unreal.StaticMeshEditorSubsystem)


def log(msg):
    unreal.log("[build_vehicles] " + msg)


# ------------------------------------------------------------------------------------------------ colour arithmetic
_F = struct.Struct("f")


def f32(z):
    return _F.unpack(_F.pack(z))[0]


_SH = 12
_SDIV = [0] + [int(round((255 << _SH) / float(i))) for i in range(1, 256)]
_HDIV = [0] + [int(round((180 << _SH) / (6.0 * i))) for i in range(1, 256)]
_K = {k: f32(v) for k, v in dict(c255=255.0, a=0.04045, b=12.92, c=0.055, d=1.055, e=2.4, g=0.0031308, i=1 / 2.4,
                                 k042=0.42).items()}


def hsv8(r, g, b):
    """8-bit HSV: hue 0-179 (2 degree steps), saturation and value 0-255."""
    v = max(r, g, b)
    diff = v - min(r, g, b)
    s = (diff * _SDIV[v] + (1 << (_SH - 1))) >> _SH
    if v == r:
        h = g - b
    elif v == g:
        h = b - r + 2 * diff
    else:
        h = r - g + 4 * diff
    h = (h * _HDIV[diff] + (1 << (_SH - 1))) >> _SH
    return (h + 180 if h < 0 else h), s, v


def s2l(c):
    return f32(c / _K["b"]) if c <= _K["a"] else f32(f32(f32(c + _K["c"]) / _K["d"]) ** _K["e"])


def l2s(x):
    return f32(x * _K["b"]) if x <= _K["g"] else f32(f32(_K["d"] * f32(max(x, 0.0) ** _K["i"])) - _K["c"])


def to8(x):
    return min(254, math.floor(f32(min(1.0, max(0.0, l2s(x))) * _K["c255"])))


def _ramp(x, a, b):
    return min(1.0, max(0.0, f32(f32(x - f32(a)) / f32(b - a))))


def car_texel(r8, g8, b8, paints):
    """Body paint texels (hue 200 +- 20..35 deg, saturation 0.20..0.35, soft edges) repainted in linear light, shading kept."""
    h, s, v = hsv8(r8, g8, b8)
    m = f32(f32(1.0 - _ramp(f32(abs(f32(h * 2.0) - 200.0)), 20.0, 35.0)) * _ramp(f32(s / _K["c255"]), 0.20, 0.35))
    lin = (s2l(f32(r8 / _K["c255"])), s2l(f32(g8 / _K["c255"])), s2l(f32(b8 / _K["c255"])))
    k = f32(s2l(f32(v / _K["c255"])) / _K["k042"])
    om = f32(1.0 - m)
    return [tuple(to8(f32(f32(lin[c] * om) + f32(f32(f32(p[c]) * k) * m))) for c in range(3)) for p in paints]


IDENTITY = [to8(s2l(f32(v / _K["c255"]))) for v in range(256)]


def van_classes(r8, g8, b8):
    h, s, v = hsv8(r8, g8, b8)
    white = v >= 0.55 * 255 and s < 0.2 * 255
    red = (h * 2 <= 20 or h * 2 >= 340) and s >= 0.4 * 255 and v >= 0.05 * 255
    return white, red


# ------------------------------------------------------------------------------------------------ image files
def png_rows(path):
    """Yields (width, height, channels, bit depth) first, then each decoded row (bytes, big-endian samples)."""
    data = open(path, "rb").read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG file: " + path)
    pos, idat, hdr = 8, [], None
    while pos < len(data):
        n, typ = struct.unpack(">I4s", data[pos:pos + 8])
        if typ == b"IHDR":
            hdr = struct.unpack(">IIBBBBB", data[pos + 8:pos + 8 + n])
        elif typ == b"IDAT":
            idat.append(data[pos + 8:pos + 8 + n])
        elif typ == b"IEND":
            break
        pos += 12 + n
    w, h, depth, ctype, _, _, interlace = hdr
    if interlace or ctype not in (2, 6) or depth not in (8, 16):
        raise ValueError("unsupported PNG (RGB or RGBA, 8 or 16 bits, not interlaced): " + path)
    ch = 3 if ctype == 2 else 4
    bpp = ch * depth // 8
    stride = w * bpp
    raw = zlib.decompress(b"".join(idat))
    yield w, h, ch, depth
    prev = bytearray(stride)
    for y in range(h):
        f = raw[y * (stride + 1)]
        cur = bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        if f == 1:
            for i in range(bpp, stride):
                cur[i] = (cur[i] + cur[i - bpp]) & 255
        elif f == 2:
            cur = bytearray((a + b) & 255 for a, b in zip(cur, prev))
        elif f == 3:
            for i in range(stride):
                cur[i] = (cur[i] + (((cur[i - bpp] if i >= bpp else 0) + prev[i]) >> 1)) & 255
        elif f == 4:
            for i in range(stride):
                a = cur[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                cur[i] = (cur[i] + (a if pa <= pb and pa <= pc else (b if pb <= pc else c))) & 255
        elif f != 0:
            raise ValueError("bad PNG filter %d in %s" % (f, path))
        yield bytes(cur)
        prev = cur


def read_tga(path):
    """(width, height, rows): rows from top to bottom, BGRA, 8 bits per channel."""
    d = open(path, "rb").read()
    idlen, cmtype, itype = d[0], d[1], d[2]
    w, h, bpp, desc = struct.unpack("<HHBB", d[12:18])
    if cmtype or itype not in (2, 10) or bpp not in (24, 32):
        raise ValueError("unsupported TGA: " + path)
    px = bpp // 8
    pos = 18 + idlen
    if itype == 2:
        body = d[pos:pos + w * h * px]
    else:
        out, need = bytearray(), w * h * px
        while len(out) < need:
            c = d[pos]
            pos += 1
            n = (c & 127) + 1
            if c & 128:
                out += d[pos:pos + px] * n
                pos += px
            else:
                out += d[pos:pos + n * px]
                pos += n * px
        body = bytes(out)
    rows = [body[y * w * px:(y + 1) * w * px] for y in range(h)]
    if not desc & 32:
        rows.reverse()
    if px == 3:
        rows = [b"".join(r[i:i + 3] + b"\xff" for i in range(0, len(r), 3)) for r in rows]
    return w, h, rows


def tga_header(w, h, bits):
    return struct.pack("<BBBHHBHHHHBB", 0, 0, 2, 0, 0, 0, 0, 0, w, h, bits, (8 if bits == 32 else 0) | 32)


# ------------------------------------------------------------------------------------------------ van UV layout
def read_obj_groups(path):
    """{group: [(uv0, uv1, uv2, y0, y1, y2), ...]}: UV triangles of each group of the OBJ with the height (y) of their vertices."""
    V, VT, groups, cur = [], [], {}, "default"
    for line in open(path, encoding="latin-1"):
        if line.startswith("v "):
            V.append(tuple(float(x) for x in line.split()[1:4]))
        elif line.startswith("vt "):
            VT.append(tuple(float(x) for x in line.split()[1:3]))
        elif line.startswith(("g ", "o ")):
            cur = line.split(None, 1)[1].strip()
        elif line.startswith("f "):
            idx = []
            for tok in line.split()[1:]:
                p = tok.split("/")
                vi, ti = int(p[0]), (int(p[1]) if len(p) > 1 and p[1] else 0)
                idx.append((vi - 1 if vi > 0 else len(V) + vi, ti - 1 if ti > 0 else len(VT) + ti))
            for k in range(1, len(idx) - 1):
                tri = (idx[0], idx[k], idx[k + 1])
                groups.setdefault(cur, []).append(tuple(VT[t] for _, t in tri) + tuple(V[v][1] for v, _ in tri))
    return groups


def rasterise(tris, w, h, out, value=None):
    """Texels whose centre lies inside a UV triangle (row 0 = v 1): out[index] = 1, or the interpolated height when value is None
    and out is a dict."""
    for (u0, v0), (u1, v1), (u2, v2), y0, y1, y2 in tris:
        x0, x1, x2 = u0 * w - 0.5, u1 * w - 0.5, u2 * w - 0.5
        r0, r1, r2 = (1 - v0) * h - 0.5, (1 - v1) * h - 0.5, (1 - v2) * h - 0.5
        den = (r1 - r2) * (x0 - x2) + (x2 - x1) * (r0 - r2)
        if den == 0:
            continue
        i_min, i_max = max(0, math.ceil(min(x0, x1, x2))), min(w - 1, math.floor(max(x0, x1, x2)))
        for j in range(max(0, math.ceil(min(r0, r1, r2))), min(h - 1, math.floor(max(r0, r1, r2))) + 1):
            for i in range(i_min, i_max + 1):
                a = ((r1 - r2) * (i - x2) + (x2 - x1) * (j - r2)) / den
                b = ((r2 - r0) * (i - x2) + (x0 - x2) * (j - r2)) / den
                c = 1.0 - a - b
                if a >= 0 and b >= 0 and c >= 0:
                    out[j * w + i] = 1 if value is not None else a * y0 + b * y1 + c * y2


# ------------------------------------------------------------------------------------------------ editor helpers
def exists(path):
    return EAL.does_asset_exist(path)


def import_file(filename, folder, name):
    t = unreal.AssetImportTask()
    t.set_editor_property("filename", filename)
    t.set_editor_property("destination_path", folder)
    t.set_editor_property("destination_name", name)
    t.set_editor_property("automated", True)
    t.set_editor_property("replace_existing", True)
    t.set_editor_property("save", True)
    AT.import_asset_tasks([t])
    a = unreal.load_asset(folder + "/" + name)
    if a is None:
        raise RuntimeError("import failed: " + filename)
    return a


def import_texture(filename, folder, name):
    tex = import_file(filename, folder, name)
    tex.set_editor_property("srgb", True)
    EAL.save_asset(tex.get_path_name())
    return tex


def sampler_for(tex):
    cs = str(tex.get_editor_property("compression_settings"))
    if "MASKS" in cs:
        return unreal.MaterialSamplerType.SAMPLERTYPE_MASKS
    if "NORMALMAP" in cs:
        return unreal.MaterialSamplerType.SAMPLERTYPE_NORMAL
    return unreal.MaterialSamplerType.SAMPLERTYPE_COLOR if tex.get_editor_property("srgb") else unreal.MaterialSamplerType.SAMPLERTYPE_LINEAR_COLOR


def texture_sample(mat, tex, x, y):
    e = MEL.create_material_expression(mat, unreal.MaterialExpressionTextureSample, x, y)
    e.set_editor_property("texture", tex)
    e.set_editor_property("sampler_type", sampler_for(tex))
    return e


def constant(mat, value, x, y):
    e = MEL.create_material_expression(mat, unreal.MaterialExpressionConstant, x, y)
    e.set_editor_property("r", value)
    return e


def car_material(name, diffuse):
    """Repainted diffuse + the pack's ORM (R ambient occlusion, G + 0.15 roughness, B metallic) and normal map, specular 0.3."""
    mat = AT.create_asset(name, OUT + "/Materiaux", unreal.Material, unreal.MaterialFactoryNew())
    orm, nrm = unreal.load_asset(HATCH_TEX + "ORM_0"), unreal.load_asset(HATCH_TEX + "Normal_0")
    MEL.connect_material_property(texture_sample(mat, diffuse, -700, -200), "RGB", unreal.MaterialProperty.MP_BASE_COLOR)
    to = texture_sample(mat, orm, -700, 200)
    MEL.connect_material_property(to, "R", unreal.MaterialProperty.MP_AMBIENT_OCCLUSION)
    add = MEL.create_material_expression(mat, unreal.MaterialExpressionAdd, -400, 200)
    add.set_editor_property("const_b", 0.15)
    MEL.connect_material_expressions(to, "G", add, "A")
    MEL.connect_material_property(add, "", unreal.MaterialProperty.MP_ROUGHNESS)
    MEL.connect_material_property(to, "B", unreal.MaterialProperty.MP_METALLIC)
    MEL.connect_material_property(constant(mat, 0.3, -400, 420), "", unreal.MaterialProperty.MP_SPECULAR)
    MEL.connect_material_property(texture_sample(mat, nrm, -700, 600), "RGB", unreal.MaterialProperty.MP_NORMAL)
    MEL.recompile_material(mat)
    EAL.save_asset(mat.get_path_name())
    return mat


def flat_material(folder, name, tex, roughness):
    mat = AT.create_asset(name, folder, unreal.Material, unreal.MaterialFactoryNew())
    MEL.connect_material_property(texture_sample(mat, tex, -700, -200), "RGB", unreal.MaterialProperty.MP_BASE_COLOR)
    MEL.connect_material_property(constant(mat, roughness, -700, 300), "", unreal.MaterialProperty.MP_ROUGHNESS)
    MEL.recompile_material(mat)
    EAL.save_asset(mat.get_path_name())
    return mat


def copy_with_body(src_path, dst_name, mat, all_slots=False):
    sm = EAL.duplicate_asset(src_path, OUT + "/" + dst_name)
    if sm is None:
        raise RuntimeError("copy failed: %s -> %s" % (src_path, dst_name))
    for i in (range(len(sm.static_materials)) if all_slots else (0,)):
        sm.set_material(i, mat)
    EAL.save_asset(sm.get_path_name())
    return sm


def merge_to_mesh(actors, name):
    """Merges actors into OUT/<name> (pivot = world origin), removes the actors, returns the static mesh."""
    opt = unreal.MergeStaticMeshActorsOptions()
    opt.set_editor_property("base_package_name", OUT + "/" + (name[3:] if name.startswith("SM_") else name))
    opt.set_editor_property("destroy_source_actors", True)
    opt.set_editor_property("new_actor_label", name + "_merged")
    opt.set_editor_property("spawn_merged_actor", True)
    ms = opt.get_editor_property("mesh_merging_settings")
    for k, v in (("pivot_type", unreal.MeshMergePivotType.WORLD_ORIGIN), ("merge_materials", False), ("merge_physics_data", False),
                 ("bake_vertex_data_to_mesh", False), ("generate_light_map_uv", False), ("computed_light_map_resolution", False),
                 ("lod_selection_type", unreal.MeshLODSelectionType.SPECIFIC_LOD), ("specific_lod", 0),
                 ("merge_equivalent_materials", True), ("allow_distance_field", True), ("include_imposters", False)):
        try:
            ms.set_editor_property(k, v)
        except Exception:
            pass
    opt.set_editor_property("mesh_merging_settings", ms)
    act = SMES.merge_static_mesh_actors(actors, opt)
    sm = None
    if act:
        sm = act.get_component_by_class(unreal.StaticMeshComponent).static_mesh
        EAS.destroy_actor(act)
    for a in actors:
        try:
            if a and a.is_valid():
                EAS.destroy_actor(a)
        except Exception:
            pass
    if sm is None and exists(OUT + "/" + name):
        sm = unreal.load_asset(OUT + "/" + name)
    if sm is None:
        raise RuntimeError("merge failed: " + name)
    EAL.save_asset(sm.get_path_name())
    return sm


def size_text(sm):
    e = sm.get_bounds().box_extent
    return "%.2f x %.2f x %.2f m" % (e.x / 50, e.y / 50, e.z / 50)


def interchange_static_pipeline():
    p = unreal.InterchangeGenericAssetsPipeline()
    for setter in (lambda: setattr(p.common_meshes_properties, "force_all_mesh_as_type", unreal.InterchangeForceMeshType.IFMT_STATIC_MESH),
                   lambda: setattr(p.mesh_pipeline, "combine_static_meshes_behavior", unreal.InterchangeCombineStaticMeshesBehavior.ALL),
                   lambda: setattr(p.mesh_pipeline, "import_skeletal_meshes", False),
                   lambda: setattr(p.common_meshes_properties, "auto_detect_mesh_type", False),
                   lambda: setattr(p.material_pipeline, "import_materials", True),
                   lambda: setattr(p.material_pipeline.texture_pipeline, "import_textures", True),
                   lambda: setattr(p.animation_pipeline, "import_animations", False)):
        try:
            setter()
        except Exception:
            pass
    o = unreal.InterchangePipelineStackOverride()
    for m in ("add_pipeline", "add_python_pipeline"):
        try:
            getattr(o, m)(p)
            return o
        except Exception:
            pass
    return None


# ------------------------------------------------------------------------------------------------ steps
def check_inputs():
    missing = []
    for p, what in ((HATCHBACK, "Vehicle Variety Pack"), (HATCH_TEX + "Diffuse_0", "Vehicle Variety Pack"),
                    (HATCH_TEX + "ORM_0", "Vehicle Variety Pack"), (HATCH_TEX + "Normal_0", "Vehicle Variety Pack"),
                    (MOTO_SRC, "Motorcycle Photoscan LOWPOLY")):
        if not exists(p):
            missing.append("%s (Fab item \"%s\")" % (p, what))
    for f in ("003.obj", "Base_Texture.png"):
        if not os.path.exists(os.path.join(VAN_DIR, f)):
            missing.append("%s (Fab item \"Standard Delivery Van\", OBJ format)" % os.path.join(VAN_DIR, f))
    if missing:
        raise RuntimeError("missing inputs:\n  " + "\n  ".join(missing))
    os.makedirs(TMP, exist_ok=True)


def cars():
    todo = [n for n in CARS if not exists(OUT + "/" + n)]
    if not todo:
        log("cars: already present")
        return
    names = sorted({CARS[n][0] for n in todo if not exists(OUT + "/Textures/TX_Hatchback_Diffuse_" + CARS[n][0])})
    if names:
        src = os.path.join(TMP, "TX_Hatchback_Diffuse_0.tga")
        task = unreal.AssetExportTask()
        task.set_editor_property("object", unreal.load_asset(HATCH_TEX + "Diffuse_0"))
        task.set_editor_property("filename", src)
        task.set_editor_property("exporter", unreal.TextureExporterTGA())
        task.set_editor_property("automated", True)
        task.set_editor_property("prompt", False)
        task.set_editor_property("replace_identical", True)
        if not unreal.Exporter.run_asset_export_task(task):
            raise RuntimeError("export of TX_Hatchback_Diffuse_0 failed")
        yield
        w, h, rows = read_tga(src)
        paints = [tuple(f32(x) for x in CAR_PAINTS[n]) for n in names]
        files = {n: open(os.path.join(TMP, "TX_Hatchback_Diffuse_%s.tga" % n), "wb") for n in names}
        for fh in files.values():
            fh.write(tga_header(w, h, 32))
        cache = {}
        t0 = time.time()
        for y, row in enumerate(rows):
            px = [row[i:i + 4] for i in range(0, len(row), 4)]
            for p in px:
                if p not in cache:
                    cache[p] = [bytes((o[2], o[1], o[0], p[3])) for o in car_texel(p[2], p[1], p[0], paints)]
            for k, n in enumerate(names):
                files[n].write(b"".join(cache[p][k] for p in px))
            if y % 64 == 63:
                if y % 1024 == 1023:
                    log("cars: repainting %d%%" % (100 * (y + 1) // h))
                yield
        for fh in files.values():
            fh.close()
        log("cars: %d texels, %d colours repainted in %.0f s" % (w * h, len(cache), time.time() - t0))
        for n in names:
            import_texture(os.path.join(TMP, "TX_Hatchback_Diffuse_%s.tga" % n), OUT + "/Textures", "TX_Hatchback_Diffuse_" + n)
            yield
    for n in todo:
        paint, mname = CARS[n]
        mpath = OUT + "/Materiaux/" + mname
        mat = unreal.load_asset(mpath) if exists(mpath) else car_material(mname, unreal.load_asset(OUT + "/Textures/TX_Hatchback_Diffuse_" + paint))
        sm = copy_with_body(HATCHBACK, n, mat)
        log("cars: %s (%s), %s" % (n, mname, size_text(sm)))
        yield


def van_base():
    vdir = OUT + "/van_livraison"
    if not exists(vdir + "/003"):
        t = unreal.AssetImportTask()
        t.set_editor_property("filename", os.path.join(VAN_DIR, "003.obj"))
        t.set_editor_property("destination_path", vdir)
        t.set_editor_property("automated", True)
        t.set_editor_property("replace_existing", True)
        t.set_editor_property("save", True)
        o = interchange_static_pipeline()
        if o is not None:
            t.set_editor_property("options", o)
        AT.import_asset_tasks([t])
        if not exists(vdir + "/003"):
            raise RuntimeError("import of 003.obj failed")
        yield
    if not exists(vdir + "/Base_Texture"):
        import_file(os.path.join(VAN_DIR, "Base_Texture.png"), vdir, "Base_Texture")
        yield
    mat = unreal.load_asset(vdir + "/M_van_livraison") if exists(vdir + "/M_van_livraison") else \
        flat_material(vdir, "M_van_livraison", unreal.load_asset(vdir + "/Base_Texture"), 0.55)
    if exists(OUT + "/SM_van_livraison"):
        log("van: SM_van_livraison already present")
        return
    a = EAS.spawn_actor_from_object(unreal.load_asset(vdir + "/003"), unreal.Vector(0, 0, 0),
                                    unreal.Rotator(pitch=0.0, yaw=VAN_YAW, roll=VAN_ROLL))
    a.set_actor_rotation(unreal.Rotator(pitch=0.0, yaw=VAN_YAW, roll=VAN_ROLL), False)
    a.set_actor_scale3d(unreal.Vector(VAN_SCALE, VAN_SCALE, VAN_SCALE))
    o, e = a.get_actor_bounds(False)
    a.set_actor_location(unreal.Vector(-o.x, -o.y, -(o.z - e.z)), False, True)
    yield
    sm = merge_to_mesh([a], "SM_van_livraison")
    for i in range(len(sm.static_materials)):
        sm.set_material(i, mat)
    EAL.save_asset(sm.get_path_name())
    log("van: SM_van_livraison, %s" % size_text(sm))
    yield


def vans():
    todo = [n for n in ("SM_van_T5", "SM_van_IDBuzz") if not exists(OUT + "/" + n)]
    if not todo:
        log("vans: already present")
        return
    textures = [t for t in ("T5", "IDBuzz") if not exists(OUT + "/Textures/Base_Texture_" + t)]
    if textures:
        groups = read_obj_groups(os.path.join(VAN_DIR, "003.obj"))
        rows = png_rows(os.path.join(VAN_DIR, "Base_Texture.png"))
        w, h, ch, depth = next(rows)
        wheel = bytearray(w * h)
        for g in VAN_WHEELS:
            rasterise(groups.get(g, []), w, h, wheel, value=1)
            yield
        height = {}
        rasterise(groups.get(VAN_BODY, []), w, h, height)
        yield
        grey, low, k_t5, k_ib = f32(T5_GREY), tuple(f32(x) for x in IDB_LOW), f32(T5_NORM), f32(IDB_NORM)
        flat_grey, flat_low, flat_high = (to8(grey),) * 3, tuple(to8(x) for x in low), (to8(f32(IDB_HIGH)),) * 3
        out = {t: open(os.path.join(TMP, "Base_Texture_%s.tga" % t), "wb") for t in ("T5", "IDBuzz")}
        for fh in out.values():
            fh.write(tga_header(w, h, 24))
        classes, vlin = {}, {}
        bps, step = ch * depth // 8, depth // 8
        t0 = time.time()
        for y in range(h):
            row = next(rows)
            l_t5, l_ib = bytearray(3 * w), bytearray(3 * w)
            for x in range(w):
                o = x * bps
                if depth == 16:
                    c16 = ((row[o] << 8) | row[o + 1], (row[o + 2] << 8) | row[o + 3], (row[o + 4] << 8) | row[o + 5])
                    c8 = (c16[0] >> 8, c16[1] >> 8, c16[2] >> 8)
                    vmax = max(c16) / 65535.0
                else:
                    c8 = (row[o], row[o + step], row[o + 2 * step])
                    vmax = max(c8) / 255.0
                cls = classes.get(c8)
                if cls is None:
                    cls = classes[c8] = van_classes(*c8)
                ident = (IDENTITY[c8[0]], IDENTITY[c8[1]], IDENTITY[c8[2]])
                t5 = ib = ident
                idx = y * w + x
                if (cls[0] or cls[1]) and not wheel[idx]:
                    is_low = height.get(idx, 1e9) < VAN_BELT
                    if cls[0]:
                        vl = vlin.get(vmax)
                        if vl is None:
                            vl = vlin[vmax] = s2l(f32(vmax))
                        t5 = (to8(f32(f32(grey * vl) / k_t5)),) * 3
                        if is_low:
                            ib = tuple(to8(f32(f32(c * vl) / k_ib)) for c in low)
                    else:
                        t5 = flat_grey
                        ib = flat_low if is_low else flat_high
                l_t5[3 * x:3 * x + 3] = bytes((t5[2], t5[1], t5[0]))
                l_ib[3 * x:3 * x + 3] = bytes((ib[2], ib[1], ib[0]))
            out["T5"].write(bytes(l_t5))
            out["IDBuzz"].write(bytes(l_ib))
            if y % 32 == 31:
                if y % 512 == 511:
                    log("vans: repainting %d%%" % (100 * (y + 1) // h))
                yield
        for fh in out.values():
            fh.close()
        log("vans: %d texels repainted in %.0f s" % (w * h, time.time() - t0))
        for t in textures:
            import_texture(os.path.join(TMP, "Base_Texture_%s.tga" % t), OUT + "/Textures", "Base_Texture_" + t)
            yield
    for n in todo:
        t = n[len("SM_van_"):]
        mpath = OUT + "/Materiaux/M_van_" + t
        mat = unreal.load_asset(mpath) if exists(mpath) else \
            flat_material(OUT + "/Materiaux", "M_van_" + t, unreal.load_asset(OUT + "/Textures/Base_Texture_" + t), 0.5)
        sm = copy_with_body(OUT + "/SM_van_livraison", n, mat)
        log("vans: %s, %s" % (n, size_text(sm)))
        yield


def motorcycle():
    if exists(OUT + "/SM_moto_v2"):
        log("motorcycle: already present")
        return
    c0 = [v / 100.0 for v in MOTO_CENTROID_CM]
    mc = [sum(MOTO_M[i][j] * c0[j] for j in range(3)) for i in range(3)]
    loc = unreal.Vector(-MOTO_SCALE * (mc[0] + MOTO_FOOTPRINT_M[0]) * 100.0, -MOTO_SCALE * (mc[1] + MOTO_FOOTPRINT_M[1]) * 100.0,
                        -MOTO_SCALE * (mc[2] + MOTO_ZMIN_M) * 100.0)
    a = EAS.spawn_actor_from_object(unreal.load_asset(MOTO_SRC), loc, unreal.Rotator(pitch=MOTO_ROT[0], yaw=MOTO_ROT[1], roll=MOTO_ROT[2]))
    a.set_actor_scale3d(unreal.Vector(MOTO_SCALE, MOTO_SCALE, MOTO_SCALE))
    yield
    sm = merge_to_mesh([a], "SM_moto_v2")
    log("motorcycle: SM_moto_v2, %s" % size_text(sm))
    yield


def all_steps():
    check_inputs()
    log("content root %s, van files %s" % (ROOT, VAN_DIR))
    for step in (cars, van_base, vans, motorcycle):
        g = step()
        if g is not None:
            for _ in g:
                yield
    log("done: " + ", ".join(n for n in list(CARS) + ["SM_van_livraison", "SM_van_T5", "SM_van_IDBuzz", "SM_moto_v2"]
                              if exists(OUT + "/" + n)))


def _tick(_dt):
    if getattr(builtins, "_build_vehicles_busy", False):
        return
    builtins._build_vehicles_busy = True
    try:
        next(builtins._build_vehicles_steps)
    except StopIteration:
        unreal.unregister_slate_post_tick_callback(builtins._build_vehicles_tick)
        builtins._build_vehicles_tick = None
    except Exception as e:
        unreal.unregister_slate_post_tick_callback(builtins._build_vehicles_tick)
        builtins._build_vehicles_tick = None
        unreal.log_error("[build_vehicles] stopped: %s" % e)
    finally:
        builtins._build_vehicles_busy = False


if getattr(builtins, "_build_vehicles_tick", None) is not None:
    log("already running")
else:
    builtins._build_vehicles_busy = False
    builtins._build_vehicles_steps = all_steps()
    builtins._build_vehicles_tick = unreal.register_slate_post_tick_callback(_tick)
