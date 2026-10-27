"""Import one baked static prop (SM_<Name>.fbx + BaseColor/NormalDX/ORM) headlessly and author M_<Name>.

Run: UnrealEditor-Cmd.exe <project> -run=pythonscript -script=<this> -NullRHI -NoHMD -unattended
Env PROP_NAME  e.g. Tent / Helmet
    PROP_SOURCE_ROOT  parent directory containing the named prop
    PROP_REPORT_DIR   directory for the generated report
    PROP_DEST  e.g. /Game/Props/Tent
    PROP_YAW   import yaw in degrees (Blender +Y comes in as UE -Y; yaw 90 turns -Y to +X)
    PROP_LOD   optional LOD group (SmallProp / LargeProp ...) -> UE auto-generates LODs
    PROP_UCX   1 = use the UCX_ hulls in the FBX as collision (no auto convex)
    PROP_DETAIL optional tileable detail normal T_<Name>_<x>.png, PROP_DETAIL_TILE = UV0 tiling
Writes <Name>_import.json to PROP_REPORT_DIR.
"""
import json, os
import unreal as u

NAME = os.environ['PROP_NAME']; DEST = os.environ['PROP_DEST']; YAW = float(os.environ.get('PROP_YAW', '0'))
LODG = os.environ.get('PROP_LOD', ''); UCX = os.environ.get('PROP_UCX', '0') == '1'
DETAIL = os.environ.get('PROP_DETAIL', ''); DTILE = float(os.environ.get('PROP_DETAIL_TILE', '1'))
SRC = os.path.join(os.environ['PROP_SOURCE_ROOT'], NAME)
REPORT = os.path.join(os.environ['PROP_REPORT_DIR'], '{}_import.json'.format(NAME))
tools = u.AssetToolsHelpers.get_asset_tools(); EAL = u.EditorAssetLibrary; MEL = u.MaterialEditingLibrary


def task(filename, name, options=None):
    t = u.AssetImportTask()
    t.set_editor_property('filename', SRC + '/' + filename); t.set_editor_property('destination_path', DEST)
    t.set_editor_property('destination_name', name); t.set_editor_property('automated', True)
    t.set_editor_property('replace_existing', True); t.set_editor_property('save', False)
    if options is not None: t.set_editor_property('options', options)
    return t


o = u.FbxImportUI()
for k, v in (('import_mesh', True), ('import_as_skeletal', False), ('import_materials', False), ('import_textures', False), ('import_animations', False)):
    o.set_editor_property(k, v)
o.set_editor_property('mesh_type_to_import', u.FBXImportType.FBXIT_STATIC_MESH)
sd = o.get_editor_property('static_mesh_import_data')
for k, v in (('combine_meshes', True), ('convert_scene', True), ('convert_scene_unit', True), ('import_uniform_scale', 1.0),
             ('auto_generate_collision', not UCX), ('one_convex_hull_per_ucx', True), ('generate_lightmap_u_vs', True)):
    sd.set_editor_property(k, v)
sd.set_editor_property('import_rotation', u.Rotator(0.0, 0.0, YAW))
sd.set_editor_property('normal_import_method', u.FBXNormalImportMethod.FBXNIM_IMPORT_NORMALS)
if UCX and EAL.does_asset_exist('%s/SM_%s' % (DEST, NAME)):
    EAL.delete_asset('%s/SM_%s' % (DEST, NAME))                    # a replace-import keeps the old body setup; start clean
tools.import_asset_tasks([task('SM_%s.fbx' % NAME, 'SM_' + NAME, o)])
sm = u.load_asset('%s/SM_%s' % (DEST, NAME))

tex = {k: 'T_%s_%s' % (NAME, k) for k in ('BaseColor', 'NormalDX', 'ORM')}
tools.import_asset_tasks([task(n + '.png', n) for n in tex.values()])
T = {k: u.load_asset(DEST + '/' + n) for k, n in tex.items()}
T['BaseColor'].set_editor_property('srgb', True)
T['NormalDX'].set_editor_property('compression_settings', u.TextureCompressionSettings.TC_NORMALMAP)
T['NormalDX'].set_editor_property('srgb', False); T['NormalDX'].set_editor_property('flip_green_channel', False)
T['ORM'].set_editor_property('compression_settings', u.TextureCompressionSettings.TC_MASKS); T['ORM'].set_editor_property('srgb', False)

mpath = '%s/M_%s' % (DEST, NAME)
mat = u.load_asset(mpath) if EAL.does_asset_exist(mpath) else tools.create_asset('M_' + NAME, DEST, u.Material, u.MaterialFactoryNew())
MEL.delete_all_material_expressions(mat)
def sample(t, st, y):
    e = MEL.create_material_expression(mat, u.MaterialExpressionTextureSample, -600, y)
    e.set_editor_property('texture', t); e.set_editor_property('sampler_type', st); return e
s_bc = sample(T['BaseColor'], u.MaterialSamplerType.SAMPLERTYPE_COLOR, -250)
s_nm = sample(T['NormalDX'], u.MaterialSamplerType.SAMPLERTYPE_NORMAL, 50)
s_or = sample(T['ORM'], u.MaterialSamplerType.SAMPLERTYPE_MASKS, 350)
MEL.connect_material_property(s_bc, 'RGB', u.MaterialProperty.MP_BASE_COLOR)
if DETAIL:
    tools.import_asset_tasks([task(DETAIL + '.png', DETAIL)])
    td = u.load_asset(DEST + '/' + DETAIL)
    td.set_editor_property('compression_settings', u.TextureCompressionSettings.TC_NORMALMAP); td.set_editor_property('srgb', False)
    tc = MEL.create_material_expression(mat, u.MaterialExpressionTextureCoordinate, -900, 650)
    tc.set_editor_property('u_tiling', DTILE); tc.set_editor_property('v_tiling', DTILE)
    s_dt = MEL.create_material_expression(mat, u.MaterialExpressionTextureSample, -600, 650)
    s_dt.set_editor_property('texture', td); s_dt.set_editor_property('sampler_type', u.MaterialSamplerType.SAMPLERTYPE_NORMAL)
    MEL.connect_material_expressions(tc, '', s_dt, 'UVs')
    bl = MEL.create_material_expression(mat, u.MaterialExpressionMaterialFunctionCall, -300, 200)
    bl.set_editor_property('material_function', u.load_asset('/Engine/Functions/Engine_MaterialFunctions02/Utility/BlendAngleCorrectedNormals'))
    MEL.connect_material_expressions(s_nm, 'RGB', bl, 'BaseNormal'); MEL.connect_material_expressions(s_dt, 'RGB', bl, 'AdditionalNormal')
    MEL.connect_material_property(bl, '', u.MaterialProperty.MP_NORMAL)
    T['Detail'] = td
else:
    MEL.connect_material_property(s_nm, 'RGB', u.MaterialProperty.MP_NORMAL)
MEL.connect_material_property(s_or, 'R', u.MaterialProperty.MP_AMBIENT_OCCLUSION)
MEL.connect_material_property(s_or, 'G', u.MaterialProperty.MP_ROUGHNESS)
MEL.connect_material_property(s_or, 'B', u.MaterialProperty.MP_METALLIC)
MEL.recompile_material(mat)
sm.set_material(0, mat)
if LODG:
    sm.set_editor_property('lod_group', LODG)
COMPLEX = os.environ.get('PROP_COMPLEX', '0') == '1'
if COMPLEX:                                        # static walk-in props: per-polygon collision, door openings stay open
    bs = sm.get_editor_property('body_setup')
    bs.set_editor_property('collision_trace_flag', u.CollisionTraceFlag.CTF_USE_COMPLEX_AS_SIMPLE)
for p in ['%s/SM_%s' % (DEST, NAME), mpath] + [DEST + '/' + n for n in tex.values()] + ([DEST + '/' + DETAIL] if DETAIL else []):
    EAL.save_asset(p, only_if_is_dirty=False)

b = sm.get_bounding_box()
rep = dict(name=NAME, dest=DEST, yaw=YAW, size_cm=[b.max.x - b.min.x, b.max.y - b.min.y, b.max.z - b.min.z],
           min=[b.min.x, b.min.y, b.min.z], max=[b.max.x, b.max.y, b.max.z], tris=sm.get_num_triangles(0),
           slot0=sm.get_material(0).get_path_name(), lods=sm.get_num_lods(),
           trace_flag=str(sm.get_editor_property('body_setup').get_editor_property('collision_trace_flag')), lod_group=str(sm.get_editor_property('lod_group')),
           collision=dict(convex=len(sm.get_editor_property('body_setup').get_editor_property('agg_geom').get_editor_property('convex_elems')),
                          boxes=len(sm.get_editor_property('body_setup').get_editor_property('agg_geom').get_editor_property('box_elems'))),
           assets=sorted(str(a) for a in EAL.list_assets(DEST, recursive=True, include_folder=False)),
           textures={k: dict(srgb=t.get_editor_property('srgb'), compression=str(t.get_editor_property('compression_settings')),
                             size=[t.blueprint_get_size_x(), t.blueprint_get_size_y()]) for k, t in T.items()})
os.makedirs(os.path.dirname(REPORT), exist_ok=True); json.dump(rep, open(REPORT, 'w'), indent=1)
print('PROP_REPORT', json.dumps(rep))
