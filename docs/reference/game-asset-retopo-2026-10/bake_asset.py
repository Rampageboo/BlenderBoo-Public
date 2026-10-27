import bpy, sys, time, numpy as np
a=sys.argv[sys.argv.index('--')+1:]; OUT=a[0]; RES=int(a[1]); NAME=a[2]; CAGE=float(a[3]); MAXD=float(a[4])
T=time.time()
sc=bpy.context.scene; vl=bpy.context.view_layer
lo=bpy.data.objects[NAME+'_low']; hi=bpy.data.objects[NAME+'_high']
hi.hide_set(False); hi.hide_render=False
for o in vl.objects: o.select_set(False)
vl.objects.active=lo; lo.select_set(True)
# low material
lo.data.materials.clear()
mat=bpy.data.materials.new("M_"+NAME); mat.use_nodes=True; lo.data.materials.append(mat)
nt=mat.node_tree; bsdf=nt.nodes['Principled BSDF']
imgs={}
def target(name, noncolor):
    im=bpy.data.images.new(f"T_{NAME}_{name}",RES,RES,alpha=False,float_buffer=False)
    im.colorspace_settings.name='Non-Color' if noncolor else 'sRGB'
    n=nt.nodes.new('ShaderNodeTexImage'); n.image=im; n.name=name
    imgs[name]=(im,n); return n
for nm,nc in [("BaseColor",False),("Normal",True),("Roughness",True),("Metallic",True),("AO",True)]: target(nm,nc)
sc.render.engine='CYCLES'
prefs=bpy.context.preferences.addons['cycles'].preferences
for dt in ('OPTIX','CUDA'):
    try:
        prefs.compute_device_type=dt; prefs.get_devices()
        for d in prefs.devices: d.use = d.type!='CPU'
        sc.cycles.device='GPU'; print("device",dt); break
    except Exception as e: print("dev fail",dt,e)
b=sc.render.bake
b.use_selected_to_active=True; b.cage_extrusion=CAGE; b.max_ray_distance=MAXD
b.margin=16; b.margin_type='EXTEND'; b.target='IMAGE_TEXTURES'
for o in vl.objects: o.select_set(False)
hi.select_set(True); lo.select_set(True); vl.objects.active=lo
def bake(name, typ, samples=8, **kw):
    t=time.time()
    for n in nt.nodes: n.select=False
    nt.nodes.active=imgs[name][1]; imgs[name][1].select=True
    sc.cycles.samples=samples
    bpy.ops.object.bake(type=typ, **kw)
    im=imgs[name][0]; im.filepath_raw=f"{OUT}/textures/T_{NAME}_{name}.png"; im.file_format='PNG'; im.save()
    print("baked",name,round(time.time()-t,1))
bake("Normal",'NORMAL',samples=1,normal_space='TANGENT')
bake("Roughness",'ROUGHNESS',samples=1)
# base color + metallic via emission on high material (DIFFUSE color bake is darkened by metallic)
hm=hi.active_material; hn=hm.node_tree; hb=hn.nodes['Principled BSDF']; outn=hn.nodes['Material Output']
orig=outn.inputs['Surface'].links[0].from_socket
em=hn.nodes.new('ShaderNodeEmission'); hn.links.new(em.outputs[0],outn.inputs['Surface'])
for name,inp in (("BaseColor","Base Color"),("Metallic","Metallic")):
    hn.links.new(hb.inputs[inp].links[0].from_socket,em.inputs['Color'])
    bake(name,'EMIT',samples=1)
hn.links.new(orig,outn.inputs['Surface']); hn.nodes.remove(em)
bake("AO",'AO',samples=64)
# ORM pack (R=AO G=Rough B=Metal) for Unreal
def px(n): return np.array(imgs[n][0].pixels[:]).reshape(-1,4)
orm=np.ones((RES*RES,4)); orm[:,0]=px("AO")[:,0]; orm[:,1]=px("Roughness")[:,0]; orm[:,2]=px("Metallic")[:,0]
im=bpy.data.images.new(f"T_{NAME}_ORM",RES,RES); im.colorspace_settings.name='Non-Color'; im.pixels[:]=orm.ravel()
im.filepath_raw=f"{OUT}/textures/T_{NAME}_ORM.png"; im.file_format='PNG'; im.save()
# DirectX normal for Unreal
nrm=px("Normal").copy(); nrm[:,1]=1-nrm[:,1]
im=bpy.data.images.new(f"T_{NAME}_Normal_DX",RES,RES); im.colorspace_settings.name='Non-Color'; im.pixels[:]=nrm.ravel()
im.filepath_raw=f"{OUT}/textures/T_{NAME}_Normal_DX.png"; im.file_format='PNG'; im.save()
# wire material
L=nt.links
L.new(imgs["BaseColor"][1].outputs[0],bsdf.inputs['Base Color'])
L.new(imgs["Roughness"][1].outputs[0],bsdf.inputs['Roughness'])
L.new(imgs["Metallic"][1].outputs[0],bsdf.inputs['Metallic'])
nm=nt.nodes.new('ShaderNodeNormalMap'); L.new(imgs["Normal"][1].outputs[0],nm.inputs['Color']); L.new(nm.outputs[0],bsdf.inputs['Normal'])
for i,(k,(im,n)) in enumerate(imgs.items()): n.location=(-700,300-i*280); im.filepath=f"{OUT}/textures/T_{NAME}_{k}.png"; im.source='FILE'
nt.nodes.remove(imgs["AO"][1])
# keep only low in output scene
bpy.data.objects.remove(hi)
for o in list(bpy.data.objects):
    if o.type=='MESH' and o!=lo: bpy.data.objects.remove(o)
bpy.ops.outliner.orphans_purge(do_recursive=True)
lo.select_set(True); vl.objects.active=lo
bpy.ops.wm.save_as_mainfile(filepath=f"{OUT}/{NAME}_game.blend",relative_remap=True)
bpy.ops.export_scene.fbx(filepath=f"{OUT}/SM_{NAME}.fbx",use_selection=True,use_mesh_modifiers=True,mesh_smooth_type="FACE",path_mode="COPY",embed_textures=False,apply_unit_scale=True,add_leaf_bones=False)
bpy.ops.export_scene.gltf(filepath=f"{OUT}/SM_{NAME}.glb",use_selection=True,export_format="GLB",export_apply=True)
print("DONE",time.time()-T, "faces",len(lo.data.polygons),"tris",sum(len(p.vertices)-2 for p in lo.data.polygons))
