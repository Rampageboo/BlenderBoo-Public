"""Fresh-process check of the imported props (run with -run=pythonscript)."""
import json, os
import unreal as u
EAL = u.EditorAssetLibrary
out = {}
for path in ('/Game/Props/Tent/SM_Tent', '/Game/Props/Helmet/SM_Helmet', '/Game/Weapons/WeaponLight/SM_WeaponLight'):
    sm = u.load_asset(path); b = sm.get_bounding_box(); bs = sm.get_editor_property('body_setup')
    mat = sm.get_material(0)
    out[path] = dict(size_cm=[round(b.max.x - b.min.x, 2), round(b.max.y - b.min.y, 2), round(b.max.z - b.min.z, 2)],
                     lods=sm.get_num_lods(), lod_tris=[sm.get_num_triangles(i) for i in range(sm.get_num_lods())],
                     lod_group=str(sm.get_editor_property('lod_group')), trace=str(bs.get_editor_property('collision_trace_flag')),
                     simple_convex=len(bs.get_editor_property('agg_geom').get_editor_property('convex_elems')),
                     material=mat.get_path_name(),
                     referenced_textures=sorted(str(r) for r in EAL.find_package_referencers_for_asset(mat.get_path_name().split('.')[0], False)))
    tex = [a for a in EAL.list_assets(path.rsplit('/', 1)[0], recursive=False, include_folder=False) if '/T_' in str(a)]
    out[path]['textures'] = {str(t).rsplit('/', 1)[-1].split('.')[0]: [u.load_asset(str(t)).blueprint_get_size_x()] for t in tex}
report_dir = os.environ['PROP_REPORT_DIR']
os.makedirs(report_dir, exist_ok=True)
with open(os.path.join(report_dir, 'verify_props.json'), 'w', encoding='utf-8') as report:
    json.dump(out, report, indent=1)
print('PROPS_VERIFY', json.dumps(out))
