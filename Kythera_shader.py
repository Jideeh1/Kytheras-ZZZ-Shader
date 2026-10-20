bl_info = {
    "name": "Kythera Shader",
    "author": "Jideeh",
    "version": (1, 7, 0),
    "blender": (5, 0, 0),
    "location": "View3D > Sidebar > Kythera",
    "description": "Swap ZZZ shaders for Kythera's ZZZ Shader and carry over textures. "
                   "Works on Setup-Wizard models and raw ZZZ FBX exports.",
    "category": "Material",
}

import os
import re
import io
import json
import time
import shutil
import zipfile
import threading
#yes we're downloading stuff from the internet inside blender, what could go wrong lol
import urllib.request
import bpy
import numpy as np
from mathutils import Matrix, Vector
from bpy.props import StringProperty, BoolProperty, PointerProperty, IntProperty, FloatProperty
from bpy.types import Operator, Panel, AddonPreferences
from bpy_extras.io_utils import ImportHelper


#constants n stuff

#setup wizard mat names and wat suffix the new mat gets
_KYSH_BODY_MATERIALS = {
    "ZZZ Shader Body": " (Body)",
    "ZZZ Shader Body 2": " (Body 2)",
    "ZZZ Shader Body3/Leg": " (Body3/Leg)",
    "ZZZ Shader Hair": " (Hair)",
    "ZZZ Shader Weapon": " (Weapon)",
    "ZZZ Shader Eye": " (Eye)",
    "ZZZ EyeHighlights": " (EyeHighlight)",
}
_KYSH_FACE_MATERIAL = "ZZZ Shader Face"
_KYSH_EYE_TRANSPARENT = "Eye Transparent"
_KYSH_BODY_PREFIX = "Kythera's ZZZ Shader V"
_KYSH_FACE_PREFIX = "Kythera's ZZZ Face Shader V"
#.001 .002 .003 blender's favorite hobby is duplicating things
_KYSH_DUP = re.compile(r"\.\d{3}$")
_KYSH_BODY_MAPS = (
    ("_D", "Body_D", "_D Map"),
    ("_M", "Body_M", "_M Map"),
    ("_A", "Body_A", "_A Map"),
    ("_N", "Body_N", "_N Map"),
)
_KYSH_MAP_SOCKETS = ("_D Map", "_M Map", "_A Map", "_N Map")
# these r data textures so no color managment on them ever
_KYSH_NONCOLOR_NODES = ("Body_M", "Body_A", "Body_N")

#raw fbx unity tex slots in the json mapped to kythera map suffix
_KYSH_JSON_SLOTS = {
    "_MainTex": "_D",
    "_OtherDataTex": "_M",
    "_OtherDataTex2": "_A",
    "_LightTex": "_N",
}
_KYSH_IMG_EXTS = (".png", ".tga", ".jpg", ".jpeg", ".dds", ".tif", ".tiff", ".bmp", ".exr")

#face direction stuff
_KYSH_GN_NAME = "Kythera ZZZ Face Vectors (Auto)"
_KYSH_GN_MOD = "Kythera Face Vectors"
_KYSH_SUN_NAME = "Kythera Sun"
_KYSH_HEAD_SUFFIX = " Head Direction"
_KYSH_HEAD_BONES = (
    "Bip001 Head", "DEF-head", "ORG-head", "head", "Head",
    "J_Bip_C_Head", "Head_M", "mixamorig:Head",
)
#sun defaults stolen straight from the Sun obj in kytheras blend lol
_KYSH_SUN_ROT = (0.8285405039787292, 0.0, -2.181661367416382)
_KYSH_SUN_ENERGY = 2.0

#what a mat ends up as
_K_BODY, _K_FACE, _K_HIDE, _K_SKIP = "BODY", "FACE", "HIDE", "SKIP"
_K_DELETE, _K_EYE = "DELETE", "EYE"

_KYSH_EYE_SHADOW = "Eye Shadow"
_KYSH_ROLE = "kysh_role"
_KYSH_OL_GN = "Kythera ZZZ Outlines"
_KYSH_OL_FAST_GN = "Kythera ZZZ Outlines (Fast)"
_KYSH_OL_MOD = "Kythera Outlines"
_KYSH_OL_FACE_MOD = "Kythera Face Outlines"
_KYSH_OL_SUFFIX = " Outline"
_KYSH_OL_HIDDEN = "Kythera Outline Hidden"
_KYSH_OL_WELD = "kysh_ol_weld"
_KYSH_OL_WIDTH = "kysh_ol_width"
_KYSH_OL_MAT = "kysh_ol_mat"
_KYSH_OL_THICKNESS = 0.075
_KYSH_OL_FACE_THICKNESS = 0.001


#lil helpers

def _kysh_strip_dup(name):
    return _KYSH_DUP.sub("", name)

#picks the newest version bc nobody deletes the old ones, me included
def _kysh_pick_latest(mats, prefix):
    best = None
    best_v = -1
    for mt in mats:
        rest = mt.name[len(prefix):]
        digits = ""
        for ch in rest:
            if ch.isdigit():
                digits += ch
            else:
                break
        v = int(digits) if digits else 0
        if v > best_v:
            best_v = v
            best = mt
    return best


def _kysh_load_sources(filepath):
    with bpy.data.libraries.load(filepath, link=False) as (data_from, data_to):
        wanted = [m for m in data_from.materials
                  if m.startswith(_KYSH_FACE_PREFIX) or m.startswith(_KYSH_BODY_PREFIX)]
        data_to.materials = wanted
    appended = [m for m in data_to.materials if m is not None]
    body_mats = [m for m in appended if m.name.startswith(_KYSH_BODY_PREFIX)]
    face_mats = [m for m in appended if m.name.startswith(_KYSH_FACE_PREFIX)]
    body_src = _kysh_pick_latest(body_mats, _KYSH_BODY_PREFIX)
    face_src = _kysh_pick_latest(face_mats, _KYSH_FACE_PREFIX)
    return appended, body_src, face_src

def _kysh_targets(context):
    return [o for o in context.scene.objects if o.type == 'MESH']


def _kysh_node_by_name(mat, name):
    if mat is None or not mat.use_nodes or mat.node_tree is None:
        return None
    for node in mat.node_tree.nodes:
        if node.type == 'TEX_IMAGE' and (node.name == name or node.label == name):
            return node
    return None


def _kysh_image_from_node(mat, name):
    node = _kysh_node_by_name(mat, name)
    return node.image if node is not None else None


def _kysh_img_base(img):
    return _KYSH_DUP.sub("", os.path.splitext(_KYSH_DUP.sub("", img.name))[0])


def _kysh_find_map_image(mat, suffix):
    if mat is None or not mat.use_nodes or mat.node_tree is None:
        return None
    for node in mat.node_tree.nodes:
        if node.type == 'TEX_IMAGE' and node.image is not None:
            if node.label and node.label.endswith(suffix):
                return node.image
    for node in mat.node_tree.nodes:
        if node.type == 'TEX_IMAGE' and node.image is not None:
            if _kysh_img_base(node.image).endswith(suffix):
                return node.image
    return None


def _kysh_find_group(mat):
    if mat is None or not mat.use_nodes or mat.node_tree is None:
        return None
    groups = [n for n in mat.node_tree.nodes if n.type == 'GROUP']
    for node in groups:
        names = [s.name for s in node.inputs]
        for sock in _KYSH_MAP_SOCKETS:
            if sock in names:
                return node
    return groups[0] if groups else None

def _kysh_link(mat, from_node, socket_name, group_node):
    if mat is None or from_node is None or group_node is None:
        return
    out = None
    for o in from_node.outputs:
        if o.name == "Color" or o.type == 'RGBA':
            out = o
            break
    if out is None and len(from_node.outputs):
        out = from_node.outputs[0]
    dst = None
    for i in group_node.inputs:
        if i.name == socket_name:
            dst = i
            break
    if out is not None and dst is not None:
        mat.node_tree.links.new(out, dst)


def _kysh_set_input(mat, socket_name, value):
    if mat is None or not mat.use_nodes or mat.node_tree is None:
        return
    for node in mat.node_tree.nodes:
        for inp in node.inputs:
            if inp.name == socket_name:
                try:
                    inp.default_value = value
                except (TypeError, AttributeError, ValueError):
                    continue


def _kysh_set_premul(img):
    if img is None:
        return
    try:
        img.alpha_mode = 'PREMUL'
    except (TypeError, AttributeError):
        pass


def _kysh_mat_tree(mat):
    try:
        if not mat.use_nodes:
            mat.use_nodes = True
    except AttributeError:
        pass
    return mat.node_tree


def _kysh_find_lightmap_image(mat):
    if mat is None or mat.node_tree is None:
        return None
    stack = [mat.node_tree]
    seen = set()
    while stack:
        tree = stack.pop()
        if tree.name in seen:
            continue
        seen.add(tree.name)
        for n in tree.nodes:
            if n.type == 'TEX_IMAGE' and n.image is not None:
                key = (n.name + n.label + n.image.name).lower().replace(" ", "").replace("_", "")
                if "lightmap" in key and "outline" not in key:
                    return n.image
            elif n.type == 'GROUP' and n.node_tree is not None:
                stack.append(n.node_tree)
    return None


def _kysh_set_colorspace(img, suffix):
    if img is None:
        return
    try:
        img.colorspace_settings.name = 'sRGB' if suffix == "_D" else 'Non-Color'
    except TypeError:
        pass
    try:
        img.alpha_mode = 'CHANNEL_PACKED'
    except (TypeError, AttributeError):
        pass


# mat builders

def _kysh_build_body(src, images, name):
    #images is dict of suffix to image, any of em can be missing its fine
    new_mat = src.copy()
    new_mat.name = name
    group = _kysh_find_group(new_mat)
    for suffix, node_name, socket_name in _KYSH_BODY_MAPS:
        node = _kysh_node_by_name(new_mat, node_name)
        if node is None:
            continue
        img = images.get(suffix)
        if img is not None:
            _kysh_set_colorspace(img, suffix)
            node.image = img
            _kysh_link(new_mat, node, socket_name, group)
    _kysh_set_input(new_mat, "Fake SSS Intensity", 1.0)
    if images.get("_N") is not None:
        #kythera's note in the shader says set normal strength to 1 if normal map connected so yea
        _kysh_set_input(new_mat, "Normal Strength", 1.0)
    _kysh_fix_material_colorspaces(new_mat)
    return new_mat


def _kysh_fix_material_colorspaces(mat):
    #force M A N body textures to non color, returns how many got changed
    changed = 0
    if mat is None or not mat.use_nodes or mat.node_tree is None:
        return 0
    for node in mat.node_tree.nodes:
        if node.type != 'TEX_IMAGE' or node.image is None:
            continue
        if node.name in _KYSH_NONCOLOR_NODES or node.label in _KYSH_NONCOLOR_NODES:
            if node.image.colorspace_settings.name != 'Non-Color':
                try:
                    node.image.colorspace_settings.name = 'Non-Color'
                    changed += 1
                except TypeError:
                    pass
    return changed


def _kysh_fix_all_colorspaces():
    return sum(_kysh_fix_material_colorspaces(m) for m in bpy.data.materials)

def _kysh_lightmap_group_node(tree):
    #finds the group node thats wrapping the face lightmap tex
    if tree is None:
        return None
    for n in tree.nodes:
        if n.type == 'GROUP' and n.node_tree is not None:
            for inner in n.node_tree.nodes:
                if inner.type == 'TEX_IMAGE':
                    return n
    return None


_KYSH_LM_CACHE = {}


def _kysh_apply_lightmap(new_mat, lightmap_img):
    # gives the face mat its own lightmap bc male n female ones r diff
    if lightmap_img is None:
        return
    _kysh_set_colorspace(lightmap_img, "_LM")
    face_group_node = _kysh_find_group(new_mat)
    if face_group_node is None or face_group_node.node_tree is None:
        return
    face_tree = face_group_node.node_tree
    inner = _kysh_lightmap_group_node(face_tree)
    if inner is None or inner.node_tree is None:
        return
    lm_tree = inner.node_tree
    tex = next((n for n in lm_tree.nodes if n.type == 'TEX_IMAGE'), None)
    if tex is not None and tex.image == lightmap_img:
        return

    key = (face_tree.name, lightmap_img.name)
    cached = _KYSH_LM_CACHE.get(key)
    if cached is not None:
        try:
            new_face_tree, new_lm_tree = cached
            new_face_tree.name  #ReferenceError check
            new_lm_tree.name
        except ReferenceError:
            cached = None
    if cached is None:
        new_lm_tree = lm_tree.copy()
        new_lm_tree.name = "Face Lightmap (%s)" % _kysh_img_base(lightmap_img)
        for n in new_lm_tree.nodes:
            if n.type == 'TEX_IMAGE':
                n.image = lightmap_img
        new_face_tree = face_tree.copy()
        new_face_tree.name = "%s (%s)" % (face_tree.name, _kysh_img_base(lightmap_img))
        new_inner = _kysh_lightmap_group_node(new_face_tree)
        if new_inner is not None:
            new_inner.node_tree = new_lm_tree
        _KYSH_LM_CACHE[key] = (new_face_tree, new_lm_tree)
    else:
        new_face_tree, new_lm_tree = cached

    face_group_node.node_tree = new_face_tree
    #the loose Face Lightmap node on the mat is just the shortcut for ppl to click into
    for n in new_mat.node_tree.nodes:
        if n.type == 'GROUP' and n.node_tree == lm_tree:
            n.node_tree = new_lm_tree


def _kysh_build_face(src, face_img, name=None, lightmap_img=None):
    new_mat = src.copy()
    new_mat.name = name or src.name
    node = _kysh_node_by_name(new_mat, "Face_D")
    if node is not None:
        if face_img is not None:
            _kysh_set_colorspace(face_img, "_D")
            _kysh_set_premul(face_img)
            node.image = face_img
        group = _kysh_find_group(new_mat)
        _kysh_link(new_mat, node, "_D Map", group)
    _kysh_apply_lightmap(new_mat, lightmap_img)
    return new_mat


def _kysh_fix_eye_transparent(mat):
    if mat is None or not mat.use_nodes or mat.node_tree is None:
        return False
    nt = mat.node_tree
    bsdf = None
    for node in nt.nodes:
        if node.type == 'GROUP' and (node.name == "Principled BSDF" or node.label == "Principled BSDF"):
            bsdf = node
            break
    if bsdf is None:
        for node in nt.nodes:
            if node.type == 'BSDF_PRINCIPLED':
                bsdf = node
                break
    if bsdf is None:
        return False
    out_node = None
    for node in nt.nodes:
        if node.type == 'OUTPUT_MATERIAL':
            out_node = node
            if node.is_active_output:
                break
    if out_node is None:
        return False
    src = None
    for o in bsdf.outputs:
        if o.type == 'SHADER' or o.name in ("BSDF", "Shader"):
            src = o
            break
    if src is None and len(bsdf.outputs):
        src = bsdf.outputs[0]
    dst = None
    for i in out_node.inputs:
        if i.name == "Surface":
            dst = i
            break
    if src is not None and dst is not None:
        nt.links.new(src, dst)
        return True
    return False


def _kysh_eye_shadow_material(face_img):
    for m in bpy.data.materials:
        if m.get(_KYSH_ROLE) != "eye_shadow" or m.node_tree is None:
            continue
        tex = next((n for n in m.node_tree.nodes if n.type == 'TEX_IMAGE'), None)
        if tex is not None and tex.image == face_img:
            return m
    mat = bpy.data.materials.new(_KYSH_EYE_SHADOW)
    mat[_KYSH_ROLE] = "eye_shadow"
    nt = _kysh_mat_tree(mat)
    nt.nodes.clear()
    tex = _kysh_new_node(nt, 'ShaderNodeTexImage', (-500, 0))
    tex.name = tex.label = "Face_D"
    tex.image = face_img
    bsdf = _kysh_new_node(nt, 'ShaderNodeBsdfTransparent', (-150, 0))
    out = _kysh_new_node(nt, 'ShaderNodeOutputMaterial', (100, 0))
    nt.links.new(tex.outputs["Color"], bsdf.inputs["Color"])
    nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
    try:
        mat.surface_render_method = 'BLENDED'
    except (AttributeError, TypeError):
        pass
    return mat


def _kysh_object_face_image(obj):
    for slot in obj.material_slots:
        m = slot.material
        if m is None or m.get(_KYSH_ROLE):
            continue
        img = _kysh_image_from_node(m, "Face_D") or _kysh_find_map_image(m, "Face_D")
        if img is not None:
            return img
    return None


#raw zzz fbx support, the MAT_ name _UI mats and the Materials json files

def _kysh_is_kythera(mat):
    n = mat.name
    return n.startswith(_KYSH_BODY_PREFIX) or n.startswith(_KYSH_FACE_PREFIX)


def _kysh_is_raw(mat):
    if mat.get(_KYSH_ROLE):
        return False
    base = _kysh_strip_dup(mat.name)
    if _kysh_is_kythera(mat) or base in _KYSH_BODY_MATERIALS or base in (_KYSH_FACE_MATERIAL, _KYSH_EYE_TRANSPARENT):
        return False
    return base.startswith("MAT_") or _kysh_find_map_image(mat, "_D") is not None


def _kysh_material_dir(mat):
    #folder where fbx textures live, we get it from the imgs the fbx importer alredy loaded
    if mat is None or not mat.use_nodes or mat.node_tree is None:
        return None
    for node in mat.node_tree.nodes:
        if node.type == 'TEX_IMAGE' and node.image is not None and node.image.filepath:
            path = bpy.path.abspath(node.image.filepath)
            folder = os.path.dirname(path)
            if os.path.isdir(folder):
                return folder
    return None

def _kysh_search_dirs(folder):
    if not folder:
        return []
    dirs = [folder]
    for sub in ("Textures", "Texture", "textures", "Tex"):
        p = os.path.join(folder, sub)
        if os.path.isdir(p):
            dirs.append(p)
    parent = os.path.dirname(folder)
    if parent and parent != folder:
        dirs.append(parent)
        for sub in ("Textures", "Texture", "textures"):
            p = os.path.join(parent, sub)
            if os.path.isdir(p):
                dirs.append(p)
    return dirs


def _kysh_find_texture_file(dirs, tex_name):
    if not tex_name:
        return None
    stem = os.path.splitext(tex_name)[0]
    for d in dirs:
        for ext in _KYSH_IMG_EXTS:
            p = os.path.join(d, stem + ext)
            if os.path.isfile(p):
                return p
    return None


def _kysh_find_lightmap_file(dirs):
    for d in dirs:
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for fn in names:
            low = fn.lower()
            if "face_lightmap" in low and low.endswith(_KYSH_IMG_EXTS):
                return os.path.join(d, fn)
    return None


def _kysh_load_image(path):
    if not path:
        return None
    try:
        return bpy.data.images.load(path, check_existing=True)
    except RuntimeError:
        return None



def _kysh_read_json(path):
    #try every encoding known to man until one works, very professional rofl
    for enc in ("utf-8-sig", "utf-16", "latin-1"):
        try:
            with open(path, "r", encoding=enc) as fh:
                return json.load(fh)
        except (UnicodeError, ValueError):
            continue
        except OSError:
            return None
    return None


def _kysh_material_json(mat, folder):
    if not folder:
        return None
    base = _kysh_strip_dup(mat.name)
    for d in (os.path.join(folder, "Materials"), folder,
              os.path.join(os.path.dirname(folder), "Materials")):
        p = os.path.join(d, base + ".json")
        if os.path.isfile(p):
            return _kysh_read_json(p)
    return None


def _kysh_json_info(data):
    #returns shader name and a dict of slot to texture name
    if not isinstance(data, dict):
        return "", {}
    shader = ""
    sh = data.get("m_Shader")
    if isinstance(sh, dict):
        shader = sh.get("Name") or ""
    tex = {}
    envs = (data.get("m_SavedProperties") or {}).get("m_TexEnvs") or {}
    if isinstance(envs, list):  #older assetstudio layout is a list of key value dicts smh
        envs = {e.get("Key") or e.get("first"): e.get("Value") or e.get("second")
                for e in envs if isinstance(e, dict)}
    for slot, val in envs.items():
        if not isinstance(val, dict):
            continue
        t = val.get("m_Texture") or {}
        name = t.get("Name") if isinstance(t, dict) else None
        if name:
            tex[slot] = name
    return shader, tex


# the sorting hat but for materials
def _kysh_classify_raw(mat, shader):
    base = _kysh_strip_dup(mat.name).lower()
    s = shader.lower()
    if "shadowcaster" in s or "stencilshadow" in s or "hairshadow" in base:
        return _K_DELETE
    if "outline" in s or "outline" in base:
        return _K_HIDE
    if s.endswith("eye") or (not s and (base.endswith("_eye") or base.endswith("_eye_ui"))):
        return _K_EYE
    if "uiface" in s or s.endswith("face") or "_face" in base or "eyebrow" in base or "brow" in base:
        return _K_FACE
    return _K_BODY


def _kysh_raw_images(mat, folder):
    #grabs D M A N imgs and the face lightmap for a raw fbx mat
    dirs = _kysh_search_dirs(folder)
    data = _kysh_material_json(mat, folder)
    shader, tex = _kysh_json_info(data)
    kind = _kysh_classify_raw(mat, shader)

    images = {}
    lightmap = None
    if tex:
        for slot, suffix in _KYSH_JSON_SLOTS.items():
            name = tex.get(slot)
            if not name:
                continue
            img = _kysh_load_image(_kysh_find_texture_file(dirs, name))
            if img is None:
                continue
            if kind == _K_FACE and slot == "_LightTex":
                lightmap = img
            else:
                images[suffix] = img

    # fallback, just go off the _D the importer loaded tbh
    d_img = images.get("_D") or _kysh_find_map_image(mat, "_D")
    if d_img is None and mat.use_nodes and mat.node_tree is not None:
        for n in mat.node_tree.nodes:
            if n.type == 'TEX_IMAGE' and n.image is not None:
                d_img = n.image
                break
    if d_img is not None and "_D" not in images:
        path = bpy.path.abspath(d_img.filepath) if d_img.filepath else ""
        images["_D"] = _kysh_load_image(path) if os.path.isfile(path) else d_img
    if images.get("_D") is not None and kind == _K_BODY:
        stem = _kysh_img_base(images["_D"])
        if stem.endswith("_D"):
            stem = stem[:-2]
            for suffix in ("_M", "_A", "_N"):
                if suffix not in images:
                    img = _kysh_load_image(_kysh_find_texture_file(dirs, stem + suffix))
                    if img is not None:
                        images[suffix] = img

    if kind == _K_FACE and lightmap is None:
        lightmap = _kysh_load_image(_kysh_find_lightmap_file(dirs))
    if kind == _K_BODY and images.get("_D") is None:
        kind = _K_SKIP
    return kind, images, lightmap

def _kysh_pretty(mat_name):
    n = _kysh_strip_dup(mat_name)
    if n.startswith("MAT_"):
        n = n[4:]
    if n.endswith("_UI"):
        n = n[:-3]
    return n


#light and geo nodes for face shader

#the addon used to forget the sun existed. flat earth shading era is over lmao
def _kysh_ensure_sun(context):
    scn = context.scene
    sun = scn.kythera_sun
    if sun is not None:
        try:
            if sun.name in bpy.data.objects and sun.type == 'LIGHT':
                if sun.name not in scn.objects:
                    scn.collection.objects.link(sun)
                sun.data.use_shadow = True
                return sun
        except ReferenceError:
            pass
    sun = bpy.data.objects.get(_KYSH_SUN_NAME)
    if sun is None or sun.type != 'LIGHT':
        data = bpy.data.lights.new(_KYSH_SUN_NAME, type='SUN')
        data.energy = _KYSH_SUN_ENERGY
        data.color = (1.0, 1.0, 1.0)
        data.angle = 0.009180432185530663
        sun = bpy.data.objects.new(_KYSH_SUN_NAME, data)
        sun.rotation_euler = _KYSH_SUN_ROT
        #location doesnt matter for a sun lamp but it looks lonely at the origin
        sun.location = (0.0, 0.0, 3.0)
    sun.data.use_shadow = True
    if sun.name not in scn.objects:
        scn.collection.objects.link(sun)
    scn.kythera_sun = sun
    return sun


#render shadows on, sun shadows on, world pitch black. the toon look hates ambient light
def _kysh_scene_setup(context):
    scn = context.scene
    eevee = getattr(scn, "eevee", None)
    if eevee is not None and hasattr(eevee, "use_shadows"):
        eevee.use_shadows = True
    _kysh_ensure_sun(context)
    world = scn.world
    if world is None:
        world = bpy.data.worlds.new("World")
        scn.world = world
    world.color = (0.0, 0.0, 0.0)
    if hasattr(world, "use_nodes") and not world.use_nodes:
        world.use_nodes = True
    nt = world.node_tree
    if nt is None:
        return
    bg = next((n for n in nt.nodes if n.type == 'BACKGROUND'), None)
    if bg is None:
        #no background node?? who did this to u
        bg = nt.nodes.new('ShaderNodeBackground')
        out = next((n for n in nt.nodes if n.type == 'OUTPUT_WORLD'), None) or nt.nodes.new('ShaderNodeOutputWorld')
        nt.links.new(bg.outputs[0], out.inputs["Surface"])
    col = bg.inputs["Color"]
    for link in list(col.links):
        nt.links.remove(link)
    col.default_value = (0.0, 0.0, 0.0, 1.0)


def _kysh_new_node(tree, idname, loc):
    n = tree.nodes.new(idname)
    n.location = loc
    return n


def _kysh_ensure_face_gn():
    #python port of Kytheras ZZZ Face Vectors but the objs r inputs now
    #outputs get stored as attrs on the face mesh n the face shader reads em
    #  SUN is the sun local +Z in world space, so pointing at the light
    #  RIGHT is head direction local +X in world
    #  FRONT is head direction local +Y in world
    # head direction empty gets made so these line up w what the shader wants
    g = bpy.data.node_groups.get(_KYSH_GN_NAME)
    if g is not None and g.bl_idname == 'GeometryNodeTree':
        return g
    g = bpy.data.node_groups.new(_KYSH_GN_NAME, 'GeometryNodeTree')
    iface = g.interface
    iface.new_socket("Geometry", in_out='INPUT', socket_type='NodeSocketGeometry')
    iface.new_socket("Sun", in_out='INPUT', socket_type='NodeSocketObject')
    iface.new_socket("Head Direction", in_out='INPUT', socket_type='NodeSocketObject')
    iface.new_socket("Geometry", in_out='OUTPUT', socket_type='NodeSocketGeometry')
    for nm in ("SUN", "RIGHT", "FRONT"):
        iface.new_socket(nm, in_out='OUTPUT', socket_type='NodeSocketVector')

    gin = _kysh_new_node(g, 'NodeGroupInput', (-700, 0))
    gout = _kysh_new_node(g, 'NodeGroupOutput', (500, 0))
    g.links.new(gin.outputs["Geometry"], gout.inputs["Geometry"])

    use_rot_node = hasattr(bpy.types, "FunctionNodeRotateVector")
    rows = (
        ("SUN", "Sun", (0.0, 0.0, 1.0), 300),
        ("RIGHT", "Head Direction", (1.0, 0.0, 0.0), -100),
        ("FRONT", "Head Direction", (0.0, 1.0, 0.0), -500),
    )
    for out_name, obj_input, axis, y in rows:
        info = _kysh_new_node(g, 'GeometryNodeObjectInfo', (-450, y))
        info.transform_space = 'ORIGINAL'
        g.links.new(gin.outputs[obj_input], info.inputs["Object"])
        vec = _kysh_new_node(g, 'ShaderNodeCombineXYZ', (-450, y + 120))
        vec.inputs[0].default_value = axis[0]
        vec.inputs[1].default_value = axis[1]
        vec.inputs[2].default_value = axis[2]
        if use_rot_node:
            rot = _kysh_new_node(g, 'FunctionNodeRotateVector', (-200, y))
            g.links.new(vec.outputs[0], rot.inputs["Vector"])
            g.links.new(info.outputs["Rotation"], rot.inputs["Rotation"])
        else:
            rot = _kysh_new_node(g, 'ShaderNodeVectorRotate', (-200, y))
            rot.rotation_type = 'EULER_XYZ'
            g.links.new(vec.outputs[0], rot.inputs["Vector"])
            g.links.new(info.outputs["Rotation"], rot.inputs["Rotation"])
        norm = _kysh_new_node(g, 'ShaderNodeVectorMath', (50, y))
        norm.operation = 'NORMALIZE'
        g.links.new(rot.outputs[0], norm.inputs[0])
        g.links.new(norm.outputs["Vector"], gout.inputs[out_name])
    return g

def _kysh_iface_id(group, name, in_out):
    for it in group.interface.items_tree:
        if it.item_type == 'SOCKET' and it.in_out == in_out and it.name == name:
            return it.identifier
    return None


def _kysh_mod_set_input(mod, ident, value):
    props = getattr(mod, "properties", None)
    if props is not None and hasattr(props, "inputs"):  #blender 5.1 and up
        sock = getattr(props.inputs, ident, None)
        if sock is not None:
            sock.value = value
            return
    mod[ident] = value  #blender 5.0 n older


def _kysh_mod_set_output_attr(mod, ident, attr):
    props = getattr(mod, "properties", None)
    if props is not None and hasattr(props, "outputs"):
        sock = getattr(props.outputs, ident, None)
        if sock is not None:
            sock.attribute_name = attr
            return
    mod[ident + "_attribute_name"] = attr


def _kysh_find_armature(obj):
    for m in obj.modifiers:
        if m.type == 'ARMATURE' and m.object is not None:
            return m.object
    p = obj.parent
    while p is not None:
        if p.type == 'ARMATURE':
            return p
        p = p.parent
    return None



def _kysh_find_head_bone(arm):
    bones = arm.data.bones
    for nm in _KYSH_HEAD_BONES:
        if nm in bones:
            return nm
    for b in bones:
        low = b.name.lower()
        if low.endswith("head") and "top" not in low and "end" not in low:
            return b.name
    return None


#looking for eyes in a skeleton, sounds cursed when u say it out loud
def _kysh_find_eye_bones(arm):
    left = right = None
    for b in arm.data.bones:
        low = b.name.lower().replace(" ", "_")
        if "eye" not in low or "brow" in low or "lid" in low:
            continue
        if left is None and ("_l_" in low or low.startswith("l_") or low.endswith("_l") or ".l" in low or "left" in low):
            left = b.name
        elif right is None and ("_r_" in low or low.startswith("r_") or low.endswith("_r") or ".r" in low or "right" in low):
            right = b.name
    return left, right


def _kysh_head_frame(arm, head_bone, face_obj):
    #world space right front up for the head
    up = Vector((0.0, 0.0, 1.0))
    right = None
    if arm is not None:
        lb, rb = _kysh_find_eye_bones(arm)
        if lb and rb:
            pl = arm.matrix_world @ arm.pose.bones[lb].head
            pr = arm.matrix_world @ arm.pose.bones[rb].head
            v = pr - pl
            if v.length > 1e-6:
                right = v.normalized()
    if right is None:
        #zzz and blender both got the char facing -Y so right is -X
        #if this runs ur character has no eyes and honestly thats a bigger problem
        right = Vector((-1.0, 0.0, 0.0))
    right = (right - up * right.dot(up))
    if right.length < 1e-6:
        right = Vector((-1.0, 0.0, 0.0))
    right.normalize()
    front = up.cross(right).normalized()
    return right, front, up


def _kysh_ensure_head_empty(face_obj):
    arm = _kysh_find_armature(face_obj)
    head_bone = _kysh_find_head_bone(arm) if arm is not None else None
    owner = arm if arm is not None else face_obj
    name = owner.name + _KYSH_HEAD_SUFFIX
    empty = bpy.data.objects.get(name)
    if empty is not None and empty.type == 'EMPTY':
        return empty

    if arm is not None and head_bone:
        loc = arm.matrix_world @ arm.pose.bones[head_bone].head
    else:
        loc = face_obj.matrix_world.translation.copy()
    right, front, up = _kysh_head_frame(arm, head_bone, face_obj)
    #kytheras face shader wants the same frame as the Face Shader obj in the demo file
    #at zero rot w the char facing -Y, so local +X is the chars LEFT and +Y is the back of the head
    # yea its backwards i tested it rofl
    x_axis = -right
    y_axis = -front
    mat = Matrix((
        (x_axis.x, y_axis.x, up.x, loc.x),
        (x_axis.y, y_axis.y, up.y, loc.y),
        (x_axis.z, y_axis.z, up.z, loc.z),
        (0.0, 0.0, 0.0, 1.0),
    ))

    empty = bpy.data.objects.new(name, None)
    empty.empty_display_type = 'SINGLE_ARROW'
    empty.empty_display_size = 0.15
    for coll in face_obj.users_collection:
        coll.objects.link(empty)
        break
    else:
        bpy.context.scene.collection.objects.link(empty)

    if arm is not None and head_bone:
        empty.parent = arm
        empty.parent_type = 'BONE'
        empty.parent_bone = head_bone
        bpy.context.view_layer.update()
        empty.matrix_world = mat
    else:
        empty.parent = face_obj
        empty.matrix_world = mat
    #nobody wants a floating arrow in their render lol
    empty.hide_render = True
    return empty


def _kysh_setup_face_object(context, obj, sun):
    group = _kysh_ensure_face_gn()
    mod = None
    for m in obj.modifiers:
        if m.type == 'NODES' and m.node_group is not None and (
                m.name == _KYSH_GN_MOD or m.node_group.name.startswith("Kythera's ZZZ Face Vectors")
                or m.node_group == group):
            mod = m
            break
    if mod is None:
        mod = obj.modifiers.new(_KYSH_GN_MOD, 'NODES')
    mod.node_group = group
    head = _kysh_ensure_head_empty(obj)
    _kysh_mod_set_input(mod, _kysh_iface_id(group, "Sun", 'INPUT'), sun)
    _kysh_mod_set_input(mod, _kysh_iface_id(group, "Head Direction", 'INPUT'), head)
    for nm in ("SUN", "RIGHT", "FRONT"):
        _kysh_mod_set_output_attr(mod, _kysh_iface_id(group, nm, 'OUTPUT'), nm)
    #keep it last so it sees the deformed mesh
    try:
        while obj.modifiers[-1] != mod:
            obj.modifiers.move(list(obj.modifiers).index(mod), len(obj.modifiers) - 1)
    except (AttributeError, RuntimeError):
        pass
    _kysh_ol_to_end(obj)
    obj.update_tag()
    return mod


def _kysh_material_is_face(mat):
    return mat is not None and mat.name.startswith(_KYSH_FACE_PREFIX)

def _kysh_ol_image(mat):
    for nm in ("Body_D", "Face_D"):
        img = _kysh_image_from_node(mat, nm)
        if img is not None:
            return img
    return _kysh_find_map_image(mat, "_D")


def _kysh_ol_finish(mat):
    try:
        mat.surface_render_method = 'DITHERED'
    except (AttributeError, TypeError):
        pass
    mat.use_backface_culling = True
    try:
        mat.use_backface_culling_shadow = True
    except AttributeError:
        pass


def _kysh_ol_hidden_material():
    mat = bpy.data.materials.get(_KYSH_OL_HIDDEN)
    if mat is not None:
        return mat
    mat = bpy.data.materials.new(_KYSH_OL_HIDDEN)
    mat[_KYSH_ROLE] = "outline"
    nt = _kysh_mat_tree(mat)
    nt.nodes.clear()
    tr = _kysh_new_node(nt, 'ShaderNodeBsdfTransparent', (-150, 0))
    out = _kysh_new_node(nt, 'ShaderNodeOutputMaterial', (100, 0))
    nt.links.new(tr.outputs[0], out.inputs["Surface"])
    _kysh_ol_finish(mat)
    return mat


def _kysh_ol_material(src):
    name = src.name + _KYSH_OL_SUFFIX
    mat = bpy.data.materials.get(name)
    if mat is not None and mat.get(_KYSH_ROLE) == "outline":
        return mat
    img = _kysh_ol_image(src)
    mat = bpy.data.materials.new(name)
    mat[_KYSH_ROLE] = "outline"
    nt = _kysh_mat_tree(mat)
    nt.nodes.clear()
    tex = _kysh_new_node(nt, 'ShaderNodeTexImage', (-750, 0))
    tex.name = tex.label = "Outline_Diffuse"
    tex.image = img
    hsv = _kysh_new_node(nt, 'ShaderNodeHueSaturation', (-450, 0))
    hsv.inputs[0].default_value = 0.5
    hsv.inputs[1].default_value = 3.0 if _kysh_material_is_face(src) else 1.2
    hsv.inputs[2].default_value = 0.4
    hsv.inputs[3].default_value = 1.0
    hsv.inputs[4].default_value = (0.1, 0.1, 0.1, 1.0)
    if img is not None:
        nt.links.new(tex.outputs["Color"], hsv.inputs[4])
    emit = _kysh_new_node(nt, 'ShaderNodeEmission', (-200, 0))
    nt.links.new(hsv.outputs[0], emit.inputs["Color"])
    lp = _kysh_new_node(nt, 'ShaderNodeLightPath', (-200, 350))
    tr = _kysh_new_node(nt, 'ShaderNodeBsdfTransparent', (-200, -150))
    mix = _kysh_new_node(nt, 'ShaderNodeMixShader', (50, 0))
    nt.links.new(lp.outputs["Is Shadow Ray"], mix.inputs[0])
    nt.links.new(emit.outputs[0], mix.inputs[1])
    nt.links.new(tr.outputs[0], mix.inputs[2])
    out = _kysh_new_node(nt, 'ShaderNodeOutputMaterial', (250, 0))
    nt.links.new(mix.outputs[0], out.inputs["Surface"])
    _kysh_ol_finish(mat)
    return mat


def _kysh_ol_skip(mat):
    if mat is None:
        return True
    role = mat.get(_KYSH_ROLE)
    if role:
        return True
    return _kysh_strip_dup(mat.name) in (_KYSH_EYE_SHADOW, _KYSH_EYE_TRANSPARENT)


def _kysh_ol_strip_slots(obj):
    mats = obj.data.materials
    for i in reversed(range(len(mats))):
        m = mats[i]
        if m is not None and m.get(_KYSH_ROLE) == "outline":
            mats.pop(index=i)


def _kysh_set_attr(me, name, dtype, domain, values):
    a = me.attributes.get(name)
    if a is not None and (a.data_type != dtype or a.domain != domain):
        me.attributes.remove(a)
        a = None
    if a is None:
        a = me.attributes.new(name, dtype, domain)
    a.data.foreach_set("value", values)


def _kysh_ol_prepare(obj):
    _kysh_ol_strip_slots(obj)
    me = obj.data
    mats = me.materials
    n = len(mats)
    lut = []
    for i in range(n):
        m = mats[i]
        if _kysh_ol_skip(m):
            mats.append(_kysh_ol_hidden_material())
            lut.append(-1)
        else:
            mats.append(_kysh_ol_material(m))
            lut.append(n + i)
    if not lut:
        lut = [-1]
    nv = len(me.vertices)
    co = np.empty(nv * 3, dtype=np.float32)
    me.vertices.foreach_get("co", co)
    key = np.round(co.reshape(-1, 3) * 100000.0).astype(np.int64)
    _u, inv = np.unique(key, axis=0, return_inverse=True)
    inv = inv.reshape(-1).astype(np.int32)
    width = np.ones(nv, dtype=np.float32)
    cols = me.color_attributes
    ca = cols.active_color if cols.active_color is not None else (cols[0] if len(cols) else None)
    if ca is not None and len(ca.data):
        raw = np.empty(len(ca.data) * 4, dtype=np.float32)
        ca.data.foreach_get("color_srgb" if ca.data_type == 'BYTE_COLOR' else "color", raw)
        red = np.clip(raw[0::4] * 2.0, 0.0, 2.0)
        if ca.domain == 'CORNER':
            vi = np.empty(len(me.loops), dtype=np.int32)
            me.loops.foreach_get("vertex_index", vi)
            tot = np.bincount(vi, weights=red, minlength=nv)
            cnt = np.bincount(vi, minlength=nv)
            width = (tot / np.maximum(cnt, 1)).astype(np.float32)
        else:
            width = red.astype(np.float32)
        gmax = np.zeros(int(inv.max()) + 1 if nv else 1, dtype=np.float32)
        np.maximum.at(gmax, inv, width)
        width = gmax[inv]
    mi = np.empty(len(me.polygons), dtype=np.int32)
    me.polygons.foreach_get("material_index", mi)
    lut = np.array(lut, dtype=np.int32)
    olm = lut[np.clip(mi, 0, len(lut) - 1)]
    _kysh_set_attr(me, _KYSH_OL_WELD, 'INT', 'POINT', inv)
    _kysh_set_attr(me, _KYSH_OL_WIDTH, 'FLOAT', 'POINT', width)
    _kysh_set_attr(me, _KYSH_OL_MAT, 'INT', 'FACE', olm)
    me.update()
    return n


def _kysh_ol_new_group(name):
    g = bpy.data.node_groups.new(name, 'GeometryNodeTree')
    iface = g.interface
    iface.new_socket("Geometry", in_out='INPUT', socket_type='NodeSocketGeometry')
    th = iface.new_socket("Thickness", in_out='INPUT', socket_type='NodeSocketFloat')
    th.default_value = _KYSH_OL_THICKNESS
    th.min_value = 0.0
    iface.new_socket("Screen Space", in_out='INPUT', socket_type='NodeSocketBool')
    iface.new_socket("Camera", in_out='INPUT', socket_type='NodeSocketObject')
    iface.new_socket("Geometry", in_out='OUTPUT', socket_type='NodeSocketGeometry')
    gin = _kysh_new_node(g, 'NodeGroupInput', (-1400, 0))
    gout = _kysh_new_node(g, 'NodeGroupOutput', (900, 0))
    return g, gin, gout


def _kysh_ol_named(g, name, dtype, loc):
    n = _kysh_new_node(g, 'GeometryNodeInputNamedAttribute', loc)
    n.data_type = dtype
    n.inputs["Name"].default_value = name
    return n


def _kysh_ol_tail(g, gin, gout, geo_socket, dir_socket):
    width = _kysh_ol_named(g, _KYSH_OL_WIDTH, 'FLOAT', (-900, -300))
    m1 = _kysh_new_node(g, 'ShaderNodeMath', (-700, -200))
    m1.operation = 'MULTIPLY'
    g.links.new(gin.outputs["Thickness"], m1.inputs[0])
    m1.inputs[1].default_value = 0.01
    m2 = _kysh_new_node(g, 'ShaderNodeMath', (-500, -200))
    m2.operation = 'MULTIPLY'
    g.links.new(m1.outputs[0], m2.inputs[0])
    g.links.new(width.outputs["Attribute"], m2.inputs[1])
    cam = _kysh_new_node(g, 'GeometryNodeObjectInfo', (-900, -550))
    cam.transform_space = 'RELATIVE'
    g.links.new(gin.outputs["Camera"], cam.inputs["Object"])
    pos = _kysh_new_node(g, 'GeometryNodeInputPosition', (-900, -750))
    dist = _kysh_new_node(g, 'ShaderNodeVectorMath', (-700, -600))
    dist.operation = 'DISTANCE'
    g.links.new(pos.outputs[0], dist.inputs[0])
    g.links.new(cam.outputs["Location"], dist.inputs[1])
    sw = _kysh_new_node(g, 'GeometryNodeSwitch', (-500, -450))
    sw.input_type = 'FLOAT'
    g.links.new(gin.outputs["Screen Space"], sw.inputs["Switch"])
    sw.inputs["False"].default_value = 1.0
    g.links.new(dist.outputs["Value"], sw.inputs["True"])
    m3 = _kysh_new_node(g, 'ShaderNodeMath', (-300, -250))
    m3.operation = 'MULTIPLY'
    g.links.new(m2.outputs[0], m3.inputs[0])
    g.links.new(sw.outputs[0], m3.inputs[1])
    scl = _kysh_new_node(g, 'ShaderNodeVectorMath', (-100, -100))
    scl.operation = 'SCALE'
    g.links.new(dir_socket, scl.inputs[0])
    g.links.new(m3.outputs[0], scl.inputs["Scale"])
    setp = _kysh_new_node(g, 'GeometryNodeSetPosition', (100, 0))
    g.links.new(geo_socket, setp.inputs["Geometry"])
    g.links.new(scl.outputs[0], setp.inputs["Offset"])
    olm = _kysh_ol_named(g, _KYSH_OL_MAT, 'INT', (100, -300))
    lt = _kysh_new_node(g, 'ShaderNodeMath', (300, -250))
    lt.operation = 'LESS_THAN'
    g.links.new(olm.outputs["Attribute"], lt.inputs[0])
    lt.inputs[1].default_value = 0.0
    dele = _kysh_new_node(g, 'GeometryNodeDeleteGeometry', (300, 0))
    dele.domain = 'FACE'
    g.links.new(setp.outputs[0], dele.inputs["Geometry"])
    g.links.new(lt.outputs[0], dele.inputs["Selection"])
    smi = _kysh_new_node(g, 'GeometryNodeSetMaterialIndex', (500, 0))
    g.links.new(dele.outputs[0], smi.inputs["Geometry"])
    g.links.new(olm.outputs["Attribute"], smi.inputs["Material Index"])
    flip = _kysh_new_node(g, 'GeometryNodeFlipFaces', (650, 0))
    g.links.new(smi.outputs[0], flip.inputs[0])
    inst = _kysh_new_node(g, 'GeometryNodeGeometryToInstance', (800, 100))
    g.links.new(gin.outputs["Geometry"], inst.inputs[0])
    g.links.new(flip.outputs[0], inst.inputs[0])
    g.links.new(inst.outputs[0], gout.inputs["Geometry"])


def _kysh_ensure_ol_gn():
    g = bpy.data.node_groups.get(_KYSH_OL_GN)
    if g is not None and g.bl_idname == 'GeometryNodeTree':
        return g
    g, gin, gout = _kysh_ol_new_group(_KYSH_OL_GN)
    merge = _kysh_new_node(g, 'GeometryNodeMergeByDistance', (-1100, 200))
    g.links.new(gin.outputs["Geometry"], merge.inputs["Geometry"])
    merge.inputs["Distance"].default_value = 0.0001
    nrm = _kysh_new_node(g, 'GeometryNodeInputNormal', (-900, 0))
    _kysh_ol_tail(g, gin, gout, merge.outputs["Geometry"], nrm.outputs["Normal"])
    return g


def _kysh_ensure_ol_fast_gn():
    g = bpy.data.node_groups.get(_KYSH_OL_FAST_GN)
    if g is not None and g.bl_idname == 'GeometryNodeTree':
        return g
    g, gin, gout = _kysh_ol_new_group(_KYSH_OL_FAST_GN)
    nrm = _kysh_new_node(g, 'GeometryNodeInputNormal', (-1100, 0))
    weld = _kysh_ol_named(g, _KYSH_OL_WELD, 'INT', (-1100, -150))
    acc = _kysh_new_node(g, 'GeometryNodeAccumulateField', (-900, 0))
    acc.data_type = 'FLOAT_VECTOR'
    acc.domain = 'POINT'
    g.links.new(nrm.outputs["Normal"], acc.inputs[0])
    g.links.new(weld.outputs["Attribute"], acc.inputs[1])
    norm = _kysh_new_node(g, 'ShaderNodeVectorMath', (-700, 0))
    norm.operation = 'NORMALIZE'
    g.links.new(acc.outputs["Total"], norm.inputs[0])
    _kysh_ol_tail(g, gin, gout, gin.outputs["Geometry"], norm.outputs["Vector"])
    return g


def _kysh_ol_is_mod(m):
    if m.type == 'NODES':
        return m.name == _KYSH_OL_MOD or (m.node_group is not None and m.node_group.name.startswith(_KYSH_OL_GN))
    return m.type == 'SOLIDIFY' and m.name == _KYSH_OL_FACE_MOD


def _kysh_ol_remove(obj):
    for m in list(obj.modifiers):
        if _kysh_ol_is_mod(m):
            obj.modifiers.remove(m)
    _kysh_ol_strip_slots(obj)
    me = obj.data
    for nm in (_KYSH_OL_WELD, _KYSH_OL_WIDTH, _KYSH_OL_MAT):
        a = me.attributes.get(nm)
        if a is not None:
            me.attributes.remove(a)


def _kysh_ol_to_end(obj):
    for m in [m for m in obj.modifiers if _kysh_ol_is_mod(m)]:
        try:
            obj.modifiers.move(list(obj.modifiers).index(m), len(obj.modifiers) - 1)
        except (AttributeError, RuntimeError, ValueError):
            pass


def _kysh_ol_sync(scn):
    scale = scn.kythera_outline_thickness / _KYSH_OL_THICKNESS
    for obj in scn.objects:
        for m in obj.modifiers:
            if not _kysh_ol_is_mod(m):
                continue
            m.show_viewport = scn.kythera_outline_viewport
            if m.type == 'SOLIDIFY':
                m.thickness = _KYSH_OL_FACE_THICKNESS * scale
                continue
            g = m.node_group
            if g is None:
                continue
            _kysh_mod_set_input(m, _kysh_iface_id(g, "Thickness", 'INPUT'), scn.kythera_outline_thickness)
            _kysh_mod_set_input(m, _kysh_iface_id(g, "Screen Space", 'INPUT'), scn.kythera_outline_screen_space)
            _kysh_mod_set_input(m, _kysh_iface_id(g, "Camera", 'INPUT'), scn.camera)
            obj.update_tag()


def _kysh_ol_update(self, context):
    _kysh_ol_sync(context.scene)


def _kysh_ol_targets(context):
    out = []
    for obj in _kysh_targets(context):
        if any(s.material is not None and _kysh_is_kythera(s.material) for s in obj.material_slots):
            out.append(obj)
    return out


def _kysh_add_outlines(context, fast):
    group = _kysh_ensure_ol_fast_gn() if fast else _kysh_ensure_ol_gn()
    done = 0
    for obj in _kysh_ol_targets(context):
        _kysh_ol_remove(obj)
        for m in obj.modifiers:
            if m.name.startswith("Outlines") and (m.type == 'SOLIDIFY' or (
                    m.type == 'NODES' and m.node_group is not None and m.node_group.name.startswith("ZZZ Outlines"))):
                m.show_viewport = False
                m.show_render = False
        n = _kysh_ol_prepare(obj)
        is_face = any(_kysh_material_is_face(s.material) for s in obj.material_slots)
        if is_face and not fast:
            mod = obj.modifiers.new(_KYSH_OL_FACE_MOD, 'SOLIDIFY')
            mod.offset = 1.0
            mod.use_flip_normals = True
            mod.use_even_offset = False
            mod.use_quality_normals = False
            mod.use_rim = False
            mod.material_offset = n
        else:
            mod = obj.modifiers.new(_KYSH_OL_MOD, 'NODES')
            mod.node_group = group
        _kysh_ol_to_end(obj)
        done += 1
    _kysh_ol_sync(context.scene)
    return done




# conversion

def _kysh_convert_object(obj, body_src, face_src, cache, raw_folder=None):
    count = 0
    has_face = False
    hide = 0
    delete = 0
    for slot in obj.material_slots:
        mat = slot.material
        if mat is None:
            continue
        if _kysh_material_is_face(mat):
            has_face = True
            continue
        if mat in cache:
            new = cache[mat]
            if new == _K_DELETE:
                delete += 1
            elif new == _K_HIDE:
                hide += 1
            elif new is not None:
                slot.material = new
                count += 1
                if _kysh_material_is_face(new):
                    has_face = True
            continue
        base = _kysh_strip_dup(mat.name)
        new = None
        if base in _KYSH_BODY_MATERIALS and body_src is not None:
            images = {sfx: _kysh_find_map_image(mat, sfx) for sfx, _n, _s in _KYSH_BODY_MAPS}
            new = _kysh_build_body(body_src, images, body_src.name + _KYSH_BODY_MATERIALS[base])
        elif base == _KYSH_FACE_MATERIAL and face_src is not None:
            lightmap = _kysh_find_lightmap_image(mat)
            if lightmap is None:
                lightmap = _kysh_load_image(_kysh_find_lightmap_file(_kysh_search_dirs(_kysh_material_dir(mat))))
            new = _kysh_build_face(face_src, _kysh_image_from_node(mat, "Face_D") or _kysh_find_map_image(mat, "_D"),
                                   lightmap_img=lightmap)
        elif base == _KYSH_EYE_TRANSPARENT:
            new = _kysh_eye_shadow_material(_kysh_object_face_image(obj))
        elif _kysh_is_raw(mat):
            folder = _kysh_material_dir(mat) or raw_folder
            kind, images, lightmap = _kysh_raw_images(mat, folder)
            label = " (%s)" % _kysh_pretty(mat.name)
            if kind == _K_FACE and face_src is not None:
                new = _kysh_build_face(face_src, images.get("_D"), face_src.name + label, lightmap)
            elif kind == _K_EYE:
                new = _kysh_eye_shadow_material(images.get("_D"))
            elif kind == _K_BODY and body_src is not None:
                new = _kysh_build_body(body_src, images, body_src.name + label)
            elif kind == _K_HIDE:
                hide += 1
                new = _K_HIDE
            elif kind == _K_DELETE:
                delete += 1
                new = _K_DELETE
        cache[mat] = new
        if new is not None and not isinstance(new, str):
            slot.material = new
            count += 1
            if _kysh_material_is_face(new):
                has_face = True
    used = len([s for s in obj.material_slots if s.material is not None])
    doomed = "hairshadow" in obj.name.lower() or (delete and delete + hide == used)
    if not doomed and hide and hide == used:
        obj.hide_set(True)
        obj.hide_render = True
    return count, has_face, doomed


def _kysh_delete_objects(objs):
    removed = 0
    for obj in objs:
        try:
            data = obj.data
            bpy.data.objects.remove(obj, do_unlink=True)
        except ReferenceError:
            continue
        removed += 1
        try:
            if data is not None and data.users == 0 and isinstance(data, bpy.types.Mesh):
                bpy.data.meshes.remove(data)
        except ReferenceError:
            pass
    return removed


def _kysh_convert_targets(context, objs, body_src, face_src, raw_folder=None):
    total = 0
    cache = {}
    face_objs = []
    doomed = []
    for obj in objs:
        n, has_face, kill = _kysh_convert_object(obj, body_src, face_src, cache, raw_folder)
        total += n
        if kill:
            doomed.append(obj)
        elif has_face:
            face_objs.append(obj)
    _kysh_delete_objects(doomed)
    if face_objs or total:
        sun = _kysh_ensure_sun(context)
        for obj in face_objs:
            _kysh_setup_face_object(context, obj, sun)
    return total, len(face_objs)


#taking out the trash, unlike me irl
def _kysh_cleanup(appended):
    for mt in appended:
        try:
            if mt.users == 0:
                bpy.data.materials.remove(mt)
        except ReferenceError:
            continue


def _kysh_apply_from_file(context, filepath, objs=None, raw_folder=None):
    if not filepath or not os.path.isfile(filepath):
        return None, None, "Kythera shader .blend not found"
    _KYSH_LM_CACHE.clear()
    appended, body_src, face_src = _kysh_load_sources(filepath)
    if body_src is None and face_src is None:
        _kysh_cleanup(appended)
        return None, None, "No Kythera shader materials found in file"
    if objs is None:
        objs = _kysh_targets(context)
    total, faces = _kysh_convert_targets(context, objs, body_src, face_src, raw_folder)
    _kysh_fix_all_colorspaces()
    _kysh_scene_setup(context)
    _kysh_cleanup(appended)
    _kysh_set_cached(context, filepath)
    return total, faces, None


#shader blend cache lives in the addon prefs now so it survives new files n restarts
#the old scene prop is only read as a fallback for scenes made w older versions
def _kysh_get_cached(context):
    prefs = _kysh_prefs()
    path = prefs.shader_path if prefs is not None else ""
    if not path:
        path = getattr(context.scene, "kythera_shader_last_file", "")
    path = bpy.path.abspath(path) if path else ""
    return path if path and os.path.isfile(path) else ""

def _kysh_cache_enabled():
    prefs = _kysh_prefs()
    return prefs.use_shader_cache if prefs is not None else True


def _kysh_set_cached(context, path):
    if not _kysh_cache_enabled():
        return
    context.scene.kythera_shader_last_file = path
    prefs = _kysh_prefs()
    if prefs is None or prefs.shader_path == path:
        return
    prefs.shader_path = path
    #save prefs right away, blender only autosaves them on quit n crashes dont count lol
    try:
        context.preferences.is_dirty = True
        if not context.preferences.use_preferences_save:
            bpy.ops.wm.save_userpref()
    except (RuntimeError, AttributeError):
        pass


def _kysh_clear_cached(context):
    context.scene.kythera_shader_last_file = ""
    prefs = _kysh_prefs()
    if prefs is not None:
        prefs.shader_path = ""
        try:
            context.preferences.is_dirty = True
            if not context.preferences.use_preferences_save:
                bpy.ops.wm.save_userpref()
        except (RuntimeError, AttributeError):
            pass


def _kysh_report(op, total, faces):
    msg = "Converted %d material slot(s)" % total
    if faces:
        msg += ", set up face vectors on %d object(s)" % faces
    op.report({'INFO'}, msg)


#operators

class JIDEEH_OT_import_kythera_shader(Operator, ImportHelper):
    bl_idname = "jideeh.import_kythera_shader"
    bl_label = "Import Kythera's Shader"
    bl_description = "Apply Kythera's shader to every mesh in the scene (Setup-Wizard or raw ZZZ FBX materials)"
    bl_options = {'REGISTER', 'UNDO'}

    filename_ext = ".blend"
    filter_glob: StringProperty(default="*.blend", options={'HIDDEN'})

    def invoke(self, context, event):
        #already know where the blend is so skip the file browser entirely
        cached = _kysh_get_cached(context)
        if _kysh_cache_enabled() and cached:
            self.filepath = cached
            return self.execute(context)
        return ImportHelper.invoke(self, context, event)

    def execute(self, context):
        path = self.filepath if self.filepath else _kysh_get_cached(context)
        total, faces, err = _kysh_apply_from_file(context, path)
        if err is not None:
            self.report({'ERROR'}, err)
            return {'CANCELLED'}
        _kysh_report(self, total, faces)
        return {'FINISHED'}


class JIDEEH_OT_kythera_import_fbx(Operator, ImportHelper):
    bl_idname = "jideeh.kythera_import_fbx"
    bl_label = "Import Raw ZZZ FBX"
    bl_description = ("Import a raw ZZZ character FBX and apply Kythera's shader, "
                      "pulling _D/_M/_A/_N maps and the face lightmap from the FBX folder")
    bl_options = {'REGISTER', 'UNDO'}

    filename_ext = ".fbx"
    filter_glob: StringProperty(default="*.fbx", options={'HIDDEN'})

    def execute(self, context):
        shader = _kysh_get_cached(context)
        if not shader:
            self.report({'ERROR'}, "Pick Kythera's shader .blend first (Import Kythera's Shader)")
            return {'CANCELLED'}
        before = set(bpy.data.objects)
        try:
            bpy.ops.import_scene.fbx(filepath=self.filepath)
        except RuntimeError as e:
            self.report({'ERROR'}, "FBX import failed: %s" % e)
            return {'CANCELLED'}
        new_objs = [o for o in bpy.data.objects if o not in before and o.type == 'MESH']
        total, faces, err = _kysh_apply_from_file(context, shader, new_objs,
                                                  os.path.dirname(self.filepath))
        if err is not None:
            self.report({'ERROR'}, err)
            return {'CANCELLED'}
        _kysh_report(self, total, faces)
        return {'FINISHED'}


class JIDEEH_OT_kythera_reapply(Operator):
    bl_idname = "jideeh.kythera_reapply"
    bl_label = "Reapply To New Objects"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        total, faces, err = _kysh_apply_from_file(context, _kysh_get_cached(context))
        if err is not None:
            self.report({'ERROR'}, err)
            return {'CANCELLED'}
        _kysh_report(self, total, faces)
        return {'FINISHED'}


class JIDEEH_OT_kythera_setup_face(Operator):
    bl_idname = "jideeh.kythera_setup_face"
    bl_label = "Fix Face Lighting"
    bl_description = ("Add the sun, the Head Direction empty and the face-vector Geometry Nodes "
                      "to every object using Kythera's face shader")
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        sun = _kysh_ensure_sun(context)
        n = 0
        for obj in _kysh_targets(context):
            if any(_kysh_material_is_face(s.material) for s in obj.material_slots):
                _kysh_setup_face_object(context, obj, sun)
                n += 1
        self.report({'INFO'}, "Face vectors set up on %d object(s)" % n)
        return {'FINISHED'}

class JIDEEH_OT_kythera_add_outlines(Operator):
    bl_idname = "jideeh.kythera_add_outlines"
    bl_label = "Add Outlines"
    bl_options = {'REGISTER', 'UNDO'}

    fast: BoolProperty(name="Fast", default=False)

    @classmethod
    def description(cls, context, props):
        if props.fast:
            return ("Inverted-hull outlines on every converted mesh with a lightweight Geometry Nodes setup. "
                    "Seams are welded once at setup instead of every frame, so posing and playback stay smooth")
        return ("Inverted-hull outlines like the HoYoverse Setup Wizard: Geometry Nodes with Merge by Distance "
                "on body meshes and a Solidify modifier on the face")

    def execute(self, context):
        n = _kysh_add_outlines(context, self.fast)
        self.report({'INFO'}, "Added outlines to %d object(s)" % n)
        return {'FINISHED'}


class JIDEEH_OT_kythera_remove_outlines(Operator):
    bl_idname = "jideeh.kythera_remove_outlines"
    bl_label = "Remove Outlines"
    bl_description = "Remove the outline modifiers and outline material slots from every mesh"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        n = 0
        for obj in _kysh_targets(context):
            if any(_kysh_ol_is_mod(m) for m in obj.modifiers):
                n += 1
            _kysh_ol_remove(obj)
        for mat in list(bpy.data.materials):
            if mat.get(_KYSH_ROLE) == "outline" and mat.users == 0:
                bpy.data.materials.remove(mat)
        self.report({'INFO'}, "Removed outlines from %d object(s)" % n)
        return {'FINISHED'}


class JIDEEH_OT_kythera_clear_cache(Operator):
    bl_idname = "jideeh.kythera_clear_cache"
    bl_label = "Clear Cache"
    bl_description = ("Forget the cached shader .blend. Next Import Kythera's Shader opens the file browser "
                      "so you can pick a newer version")
    bl_options = {'REGISTER'}

    def execute(self, context):
        _kysh_clear_cached(context)
        self.report({'INFO'}, "Cleared cached shader file, next import will ask for it again")
        return {'FINISHED'}


class JIDEEH_OT_kythera_select_unconverted(Operator):
    bl_idname = "jideeh.kythera_select_unconverted"
    bl_label = "Select Unconverted"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        for o in context.scene.objects:
            o.select_set(False)
        hits = 0
        last = None
        for obj in context.scene.objects:
            if obj.type != 'MESH' or not obj.visible_get():
                continue
            for slot in obj.material_slots:
                if slot.material is None:
                    continue
                base = _kysh_strip_dup(slot.material.name)
                if base in _KYSH_BODY_MATERIALS or base == _KYSH_FACE_MATERIAL or base.startswith("MAT_"):
                    obj.select_set(True)
                    last = obj
                    hits += 1
                    break
        if last is not None:
            context.view_layer.objects.active = last
        self.report({'INFO'}, "%d object(s) still using original ZZZ materials" % hits)
        return {'FINISHED'}


class JIDEEH_OT_kythera_purge_old(Operator):
    bl_idname = "jideeh.kythera_purge_old"
    bl_label = "Purge Old ZZZ Materials"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        removed = 0
        for mat in list(bpy.data.materials):
            base = _kysh_strip_dup(mat.name)
            if base in _KYSH_BODY_MATERIALS or base == _KYSH_FACE_MATERIAL or base.startswith("MAT_"):
                mat.use_fake_user = False
                if mat.users == 0:
                    bpy.data.materials.remove(mat)
                    removed += 1
        self.report({'INFO'}, "Removed %d unused material(s)" % removed)
        return {'FINISHED'}


class JIDEEH_OT_kythera_fix_colorspace(Operator):
    bl_idname = "jideeh.kythera_fix_colorspace"
    bl_label = "Fix Texture Color Spaces"
    bl_description = "Set every Body_M, Body_A and Body_N image texture to Non-Color"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        n = _kysh_fix_all_colorspaces()
        self.report({'INFO'}, "Set %d image(s) to Non-Color" % n)
        return {'FINISHED'}


#auto updater from github releases
#release format it understands, pick one per release
#  a py asset, thats just the addon file like kythera_shader.py
#  a zip asset w the addon py inside or a package folder
#  no asset at all, then it uses githubs source zip for the tag
#tag needs the version in it like v1.6.2 and it shud match bl_info version or itll keep nagging u

_KYSH_REPO = "Jideeh1/Kythera-s-Shader"
_KYSH_API = "https://api.github.com/repos/%s/releases" % _KYSH_REPO
_KYSH_RELEASES_PAGE = "https://github.com/%s/releases" % _KYSH_REPO
_KYSH_UA = "Kythera-Shader-Updater"

#shared between worker thread n the ui, dont touch bpy from the thread ever or blender goes boom
_KYSH_UPD = {
    "busy": False,
    "status": "",
    "latest": None,        #version tuple
    "latest_tag": "",
    "notes": "",
    "release": None,       #raw release dict
    "installed": None,     #version tuple thats on disk waitin for a reload
    "error": "",
}


def _kysh_current_version():
    return tuple(bl_info.get("version", (0, 0, 0)))


def _kysh_parse_version(tag):
    nums = re.findall(r"\d+", tag or "")
    if not nums:
        return None
    v = tuple(int(n) for n in nums[:3])
    return v + (0,) * (3 - len(v))

def _kysh_vstr(v):
    return ".".join(str(x) for x in v) if v else "?"


#github pls dont rate limit me i only asked 60 times
def _kysh_http_get(url, accept="application/vnd.github+json", timeout=20):
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": _KYSH_UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _kysh_fetch_latest(include_pre):
    data = json.loads(_kysh_http_get(_KYSH_API + "?per_page=20").decode("utf-8"))
    best = None
    best_v = None
    for rel in data:
        if rel.get("draft"):
            continue
        if rel.get("prerelease") and not include_pre:
            continue
        v = _kysh_parse_version(rel.get("tag_name") or rel.get("name"))
        if v is None:
            continue
        if best_v is None or v > best_v:
            best, best_v = rel, v
    return best, best_v


def _kysh_addon_target():
    #path and is_package of the installed addon
    path = os.path.abspath(__file__)
    is_pkg = os.path.basename(path) == "__init__.py"
    return (os.path.dirname(path) if is_pkg else path), is_pkg


#making sure we dont install a 404 page as python code lmfao
def _kysh_valid_addon_source(src_bytes):
    try:
        text = src_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return False
    if "bl_info" not in text or "def register" not in text:
        return False
    try:
        compile(text, "kythera_update", "exec")
    except SyntaxError:
        return False
    return True


def _kysh_pick_from_zip(zbytes):
    #gives back file plus bytes for a single py, or pkg plus zipfile plus prefix for a package
    zf = zipfile.ZipFile(io.BytesIO(zbytes))
    names = [n for n in zf.namelist() if not n.endswith("/")]
    my_name = os.path.basename(os.path.abspath(__file__))
    #first try a package folder w an __init__ that has bl_info
    inits = sorted((n for n in names if n.endswith("__init__.py")), key=lambda n: n.count("/"))
    for n in inits:
        data = zf.read(n)
        if _kysh_valid_addon_source(data):
            return ("pkg", zf, n[: -len("__init__.py")])
    # then a single file addon, prefer one w the same name as the installed one
    pys = sorted((n for n in names if n.endswith(".py")),
                 key=lambda n: (os.path.basename(n) != my_name, n.count("/")))
    for n in pys:
        data = zf.read(n)
        if _kysh_valid_addon_source(data):
            return ("file", data)
    return None


def _kysh_download_release(rel):
    assets = rel.get("assets") or []
    py_assets = [a for a in assets if a.get("name", "").lower().endswith(".py")]
    zip_assets = [a for a in assets if a.get("name", "").lower().endswith(".zip")]
    if py_assets:
        data = _kysh_http_get(py_assets[0]["browser_download_url"], accept="application/octet-stream")
        if _kysh_valid_addon_source(data):
            return ("file", data)
    for a in zip_assets:
        data = _kysh_http_get(a["browser_download_url"], accept="application/octet-stream")
        picked = _kysh_pick_from_zip(data)
        if picked:
            return picked
    if rel.get("zipball_url"):
        picked = _kysh_pick_from_zip(_kysh_http_get(rel["zipball_url"], accept="application/zip", timeout=60))
        if picked:
            return picked
    raise RuntimeError("Release has no usable add-on file")


def _kysh_install_payload(payload):
    target, is_pkg = _kysh_addon_target()
    if payload[0] == "file":
        data = payload[1]
        dest = os.path.join(target, "__init__.py") if is_pkg else target
        if os.path.isfile(dest):
            #backup just in case i push a broken release at 3am again
            shutil.copy2(dest, dest + ".bak")
        tmp = dest + ".new"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, dest)
        return
    _kind, zf, prefix = payload
    if not is_pkg:
        #installed as single file but release is a package, just yoink its __init__ lol
        data = zf.read(prefix + "__init__.py")
        shutil.copy2(target, target + ".bak")
        with open(target + ".new", "wb") as fh:
            fh.write(data)
        os.replace(target + ".new", target)
        return
    for n in zf.namelist():
        if not n.startswith(prefix) or n.endswith("/"):
            continue
        rel_path = n[len(prefix):]
        #no escaping the addon folder thank u very much
        if not rel_path or ".." in rel_path.split("/"):
            continue
        dest = os.path.join(target, *rel_path.split("/"))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if os.path.isfile(dest) and rel_path == "__init__.py":
            shutil.copy2(dest, dest + ".bak")
        with open(dest, "wb") as fh:
            fh.write(zf.read(n))


def _kysh_worker(include_pre, install):
    st = _KYSH_UPD
    try:
        st["status"] = "Checking GitHub..."
        rel, v = _kysh_fetch_latest(include_pre)
        if rel is None:
            st["latest"], st["release"] = None, None
            st["status"] = "No releases published yet"
            return
        st["latest"], st["latest_tag"], st["release"] = v, rel.get("tag_name", ""), rel
        st["notes"] = (rel.get("body") or "").strip()
        current = st["installed"] or _kysh_current_version()
        if v <= current:
            st["status"] = "Up to date (v%s)" % _kysh_vstr(current)
            return
        if not install:
            st["status"] = "Update available: v%s" % _kysh_vstr(v)
            return
        st["status"] = "Downloading v%s..." % _kysh_vstr(v)
        payload = _kysh_download_release(rel)
        _kysh_install_payload(payload)
        st["installed"] = v
        st["status"] = "Installed v%s - restart Blender (or Reload) to finish" % _kysh_vstr(v)
    except Exception as e:  #network dying, rate limit, busted zip etc idk
        st["error"] = str(e)
        st["status"] = "Update failed: %s" % e
    finally:
        st["busy"] = False

def _kysh_prefs():
    addon = bpy.context.preferences.addons.get(__name__)
    return addon.preferences if addon is not None else None


def _kysh_start_update(include_pre, install):
    if _KYSH_UPD["busy"]:
        return False
    _KYSH_UPD["busy"] = True
    _KYSH_UPD["error"] = ""
    threading.Thread(target=_kysh_worker, args=(include_pre, install), daemon=True).start()
    bpy.app.timers.register(_kysh_redraw_while_busy, first_interval=0.5)
    return True


def _kysh_redraw_while_busy():
    try:
        for win in bpy.context.window_manager.windows:
            for area in win.screen.areas:
                if area.type in ('VIEW_3D', 'PREFERENCES'):
                    area.tag_redraw()
    except (AttributeError, ReferenceError):
        pass
    return 0.5 if _KYSH_UPD["busy"] else None


def _kysh_startup_check():
    prefs = _kysh_prefs()
    if prefs is None or not prefs.auto_check:
        return None
    now = time.time()
    if now - prefs.last_check < prefs.check_interval_hours * 3600:
        return None
    prefs.last_check = now
    _kysh_start_update(prefs.include_prerelease, prefs.auto_install)
    return None


#turn it off and on again, the ancient IT ritual
def _kysh_reload_addon():
    import addon_utils
    import sys
    name = __name__
    #disabling an addon wipes its prefs so stash em first or the shader cache goes poof
    saved = {}
    prefs = _kysh_prefs()
    if prefs is not None:
        for key in ("shader_path", "use_shader_cache", "auto_check", "auto_install",
                    "include_prerelease", "check_interval_hours", "last_check"):
            saved[key] = getattr(prefs, key)
    addon_utils.disable(name, default_set=True)  #brb
    sys.modules.pop(name, None)  #make sure we actually load the new file n not the old one in memory
    addon_utils.enable(name, default_set=True)  #reimports bc the file changed ngl
    addon = bpy.context.preferences.addons.get(name)
    if addon is not None and saved:
        for key, val in saved.items():
            try:
                setattr(addon.preferences, key, val)
            except (AttributeError, TypeError):
                pass
        try:
            bpy.context.preferences.is_dirty = True
            if not bpy.context.preferences.use_preferences_save:
                bpy.ops.wm.save_userpref()
        except RuntimeError:
            pass
    return None


class JIDEEH_OT_kythera_check_update(Operator):
    bl_idname = "jideeh.kythera_check_update"
    bl_label = "Check for Updates"
    bl_description = "Look for a newer Kythera Shader release on GitHub"

    def execute(self, context):
        prefs = _kysh_prefs()
        pre = prefs.include_prerelease if prefs else False
        if prefs:
            prefs.last_check = time.time()
        if not _kysh_start_update(pre, install=False):
            self.report({'WARNING'}, "Already checking")
        return {'FINISHED'}


class JIDEEH_OT_kythera_install_update(Operator):
    bl_idname = "jideeh.kythera_install_update"
    bl_label = "Install Update"
    bl_description = "Download the latest release from GitHub and replace this add-on (a .bak copy is kept)"

    def execute(self, context):
        prefs = _kysh_prefs()
        pre = prefs.include_prerelease if prefs else False
        if not _kysh_start_update(pre, install=True):
            self.report({'WARNING'}, "Updater is busy")
        return {'FINISHED'}


class JIDEEH_OT_kythera_reload_addon(Operator):
    bl_idname = "jideeh.kythera_reload_addon"
    bl_label = "Reload Add-on"
    bl_description = "Reload the add-on so the downloaded update takes effect without restarting Blender"

    def execute(self, context):
        bpy.app.timers.register(_kysh_reload_addon, first_interval=0.1)
        return {'FINISHED'}



class JIDEEH_OT_kythera_open_releases(Operator):
    bl_idname = "jideeh.kythera_open_releases"
    bl_label = "Open Releases Page"

    def execute(self, context):
        bpy.ops.wm.url_open(url=_KYSH_RELEASES_PAGE)
        return {'FINISHED'}


def _kysh_draw_updater(layout):
    st = _KYSH_UPD
    col = layout.column(align=True)
    col.label(text="Installed: v%s" % _kysh_vstr(_kysh_current_version()), icon='PLUGIN')
    if st["status"]:
        icon = 'ERROR' if st["error"] else ('SORTTIME' if st["busy"] else 'INFO')
        col.label(text=st["status"], icon=icon)
    row = col.row(align=True)
    row.enabled = not st["busy"]
    row.operator("jideeh.kythera_check_update", icon='FILE_REFRESH')
    current = st["installed"] or _kysh_current_version()
    if st["latest"] and st["latest"] > current:
        r = col.row(align=True)
        r.enabled = not st["busy"]
        r.scale_y = 1.3
        r.operator("jideeh.kythera_install_update", text="Install v%s" % _kysh_vstr(st["latest"]), icon='IMPORT')
    if st["installed"]:
        r = col.row(align=True)
        r.alert = True
        r.operator("jideeh.kythera_reload_addon", icon='FILE_REFRESH')
    col.operator("jideeh.kythera_open_releases", icon='URL')


class KytheraShaderPreferences(AddonPreferences):
    bl_idname = __name__

    auto_check: BoolProperty(
        name="Check for updates automatically",
        description="Check GitHub for a new release when Blender starts",
        default=True,
    )
    auto_install: BoolProperty(
        name="Install updates automatically",
        description="Download and install new releases as soon as they are found "
                    "(takes effect after restarting Blender)",
        default=True,
    )
    include_prerelease: BoolProperty(
        name="Include pre-releases",
        default=False,
    )
    check_interval_hours: IntProperty(
        name="Check every (hours)",
        default=12, min=0, max=24 * 30,
        description="0 = every time Blender starts",
    )
    last_check: FloatProperty(default=0.0, options={'HIDDEN'})
    shader_path: StringProperty(
        name="Shader .blend",
        description="Cached Kythera's shader file, used by every import without asking again",
        subtype='FILE_PATH',
        default="",
    )
    use_shader_cache: BoolProperty(
        name="Remember shader file",
        description="Skip the file browser on Import Kythera's Shader once a file was picked",
        default=True,
    )

    def draw(self, context):
        layout = self.layout
        sbox = layout.box()
        sbox.label(text="Shader file", icon='BLENDER')
        sbox.prop(self, "use_shader_cache")
        row = sbox.row(align=True)
        row.prop(self, "shader_path", text="")
        row.operator("jideeh.kythera_clear_cache", text="", icon='X')
        box = layout.box()
        box.label(text="Updates", icon='WORLD')
        box.prop(self, "auto_check")
        sub = box.column()
        sub.enabled = self.auto_check
        sub.prop(self, "auto_install")
        sub.prop(self, "check_interval_hours")
        box.prop(self, "include_prerelease")
        _kysh_draw_updater(box)


#ui

def _kysh_draw_subpanel(layout, data, prop_name, label, icon):
    box = layout.box()
    row = box.row()
    row.alignment = 'LEFT'
    row.prop(
        data,
        prop_name,
        icon='TRIA_DOWN' if getattr(data, prop_name) else 'TRIA_RIGHT',
        icon_only=True,
        emboss=False,
    )
    row.label(text=label, icon=icon)
    return box, getattr(data, prop_name)


class JIDEEH_PT_kythera_shader(Panel):
    bl_label = "Kythera Shader"
    bl_idname = "JIDEEH_PT_kythera_shader"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Kythera"

    def draw(self, context):
        layout = self.layout
        scn = context.scene
        menu, menu_open = _kysh_draw_subpanel(layout, scn, "kythera_shader_show_menu", "Kythera Shader", 'SHADING_RENDERED')
        if not menu_open:
            return
        st = _KYSH_UPD
        current = st["installed"] or _kysh_current_version()
        if st["installed"]:
            warn = menu.row()
            warn.alert = True
            warn.operator("jideeh.kythera_reload_addon",
                          text="v%s installed - Reload" % _kysh_vstr(st["installed"]), icon='FILE_REFRESH')
        elif st["latest"] and st["latest"] > current:
            menu.operator("jideeh.kythera_install_update",
                          text="Update to v%s" % _kysh_vstr(st["latest"]), icon='IMPORT')
        col = menu.column(align=True)
        row = col.row(align=True)
        row.scale_y = 1.5
        row.operator("jideeh.import_kythera_shader", icon='IMPORT')
        row = col.row(align=True)
        row.operator("jideeh.kythera_import_fbx", icon='ARMATURE_DATA')
        row = col.row(align=True)
        row.operator("jideeh.kythera_reapply", icon='FILE_REFRESH')
        cached = _kysh_get_cached(context)
        row = menu.row(align=True)
        if cached:
            row.label(text=os.path.basename(cached), icon='BLENDER')
            row.operator("jideeh.kythera_clear_cache", text="", icon='X')
        else:
            row.label(text="No shader cached yet", icon='QUESTION')

        box, expanded = _kysh_draw_subpanel(menu, scn, "kythera_shader_show_light", "Light", 'LIGHT_SUN')
        if expanded:
            box.prop(scn, "kythera_sun", text="Sun")
            sun = scn.kythera_sun
            if sun is not None and sun.type == 'LIGHT':
                box.prop(sun, "rotation_euler", text="Direction")
                box.prop(sun.data, "energy")
            box.operator("jideeh.kythera_setup_face", icon='FACESEL')

        box, expanded = _kysh_draw_subpanel(menu, scn, "kythera_shader_show_outlines", "Outlines", 'MOD_SOLIDIFY')
        if expanded:
            col = box.column(align=True)
            op = col.operator("jideeh.kythera_add_outlines", text="Add Outlines", icon='MOD_SOLIDIFY')
            op.fast = False
            op = col.operator("jideeh.kythera_add_outlines", text="Add Fast Outlines", icon='FF')
            op.fast = True
            col.operator("jideeh.kythera_remove_outlines", icon='X')
            box.prop(scn, "kythera_outline_thickness", text="Thickness")
            row = box.row(align=True)
            row.prop(scn, "kythera_outline_screen_space", text="Screen Space", toggle=True)
            row.prop(scn, "kythera_outline_viewport", text="Viewport", toggle=True, icon='RESTRICT_VIEW_OFF')

        box, expanded = _kysh_draw_subpanel(menu, scn, "kythera_shader_show_tools", "Tools", 'TOOL_SETTINGS')
        if expanded:
            tools = box.column(align=True)
            tools.operator("jideeh.kythera_select_unconverted", icon='RESTRICT_SELECT_OFF')
            tools.operator("jideeh.kythera_purge_old", icon='TRASH')
            tools.operator("jideeh.kythera_fix_colorspace", icon='IMAGE_DATA')
            row = box.row(align=True)
            row.operator("jideeh.kythera_clear_cache", text="Clear Cache", icon='TRASH')
            prefs = _kysh_prefs()
            if prefs is not None:
                row.prop(prefs, "use_shader_cache", text="Cache")

        box, expanded = _kysh_draw_subpanel(menu, scn, "kythera_shader_show_update", "Updates", 'WORLD')
        if expanded:
            _kysh_draw_updater(box)
            prefs = _kysh_prefs()
            if prefs is not None:
                box.prop(prefs, "auto_check", text="Auto-check")
                box.prop(prefs, "auto_install", text="Auto-install")


_KYSH_CLASSES = (
    JIDEEH_OT_import_kythera_shader,
    JIDEEH_OT_kythera_import_fbx,
    JIDEEH_OT_kythera_reapply,
    JIDEEH_OT_kythera_setup_face,
    JIDEEH_OT_kythera_add_outlines,
    JIDEEH_OT_kythera_remove_outlines,
    JIDEEH_OT_kythera_clear_cache,
    JIDEEH_OT_kythera_select_unconverted,
    JIDEEH_OT_kythera_purge_old,
    JIDEEH_OT_kythera_fix_colorspace,
    JIDEEH_OT_kythera_check_update,
    JIDEEH_OT_kythera_install_update,
    JIDEEH_OT_kythera_reload_addon,
    JIDEEH_OT_kythera_open_releases,
    KytheraShaderPreferences,
    JIDEEH_PT_kythera_shader,
)


def _kysh_poll_sun(self, obj):
    return obj.type == 'LIGHT'


def register():
    bpy.types.Scene.kythera_shader_last_file = StringProperty(
        name="Last Shader File",
        default="",
        subtype='FILE_PATH',
    )
    bpy.types.Scene.kythera_shader_use_cache = BoolProperty(
        name="Cache",
        default=True,
    )
    bpy.types.Scene.kythera_shader_show_menu = BoolProperty(
        name="Kythera Shader",
        default=True,
    )
    bpy.types.Scene.kythera_shader_show_tools = BoolProperty(
        name="Tools",
        default=False,
    )
    bpy.types.Scene.kythera_shader_show_light = BoolProperty(
        name="Light",
        default=True,
    )
    bpy.types.Scene.kythera_sun = PointerProperty(
        name="Kythera Sun",
        type=bpy.types.Object,
        poll=_kysh_poll_sun,
        description="Sun lamp that drives the toon shading and the face shadow direction",
    )
    bpy.types.Scene.kythera_shader_show_update = BoolProperty(
        name="Updates",
        default=False,
    )
    bpy.types.Scene.kythera_shader_show_outlines = BoolProperty(
        name="Outlines",
        default=True,
    )
    bpy.types.Scene.kythera_outline_thickness = FloatProperty(
        name="Outline Thickness",
        default=_KYSH_OL_THICKNESS,
        min=0.0,
        soft_max=0.5,
        step=0.5,
        precision=3,
        update=_kysh_ol_update,
    )
    bpy.types.Scene.kythera_outline_screen_space = BoolProperty(
        name="Screen Space",
        description="Scale outline width by distance to the scene camera so it stays the same size on screen",
        default=False,
        update=_kysh_ol_update,
    )
    bpy.types.Scene.kythera_outline_viewport = BoolProperty(
        name="Show Outlines In Viewport",
        description="Turn off to hide outlines in the viewport while posing, they still render",
        default=True,
        update=_kysh_ol_update,
    )
    for cls in _KYSH_CLASSES:
        bpy.utils.register_class(cls)
    #delay it so prefs r loaded b4 we read em
    bpy.app.timers.register(_kysh_startup_check, first_interval=3.0, persistent=False)


def unregister():
    if bpy.app.timers.is_registered(_kysh_startup_check):
        bpy.app.timers.unregister(_kysh_startup_check)
    if bpy.app.timers.is_registered(_kysh_redraw_while_busy):
        bpy.app.timers.unregister(_kysh_redraw_while_busy)
    for cls in reversed(_KYSH_CLASSES):
        bpy.utils.unregister_class(cls)
    del bpy.types.Scene.kythera_outline_viewport
    del bpy.types.Scene.kythera_outline_screen_space
    del bpy.types.Scene.kythera_outline_thickness
    del bpy.types.Scene.kythera_shader_show_outlines
    del bpy.types.Scene.kythera_shader_show_update
    del bpy.types.Scene.kythera_sun
    del bpy.types.Scene.kythera_shader_show_light
    del bpy.types.Scene.kythera_shader_show_tools
    del bpy.types.Scene.kythera_shader_show_menu
    del bpy.types.Scene.kythera_shader_use_cache
    del bpy.types.Scene.kythera_shader_last_file


if __name__ == "__main__":
    register()