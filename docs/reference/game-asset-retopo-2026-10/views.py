import bpy, sys, math, json
from mathutils import Vector
a=sys.argv[sys.argv.index('--')+1:]; out=a[0]; name=a[1]; wire=a[2]=='wire'; views=json.loads(a[3])
sc=bpy.context.scene
for x in sc.objects:
    if x.type=='MESH': x.hide_render = x.name!=name; x.hide_set(x.name!=name)
o=bpy.data.objects[name]
sc.render.engine='BLENDER_WORKBENCH'; sh=sc.display.shading; sh.light='MATCAP'; sh.color_type='SINGLE'; sh.single_color=(0.6,0.6,0.6)
if wire:
    w=o.copy(); w.data=o.data.copy(); sc.collection.objects.link(w); w.hide_render=False; w.hide_set(False)
    md=w.modifiers.new("w",'WIREFRAME'); md.thickness=float(a[4]) if len(a)>4 else 0.0008; md.use_replace=True; w.color=(0,0,0,1)
    sh.color_type='OBJECT'; o.color=(0.6,0.6,0.6,1)
sc.render.resolution_x=1200; sc.render.resolution_y=900
cam=bpy.data.objects.get('QAcam') or bpy.data.objects.new('QAcam',bpy.data.cameras.new('QAcam'))
if cam.name not in sc.collection.objects: sc.collection.objects.link(cam)
sc.camera=cam; cam.data.lens=50; cam.data.clip_start=0.001
for nm,(tx,ty,tz),(dx,dy,dz),dist in views:
    T=Vector((tx,ty,tz)); d=Vector((dx,dy,dz)).normalized(); cam.location=T+d*dist
    cam.rotation_euler=(T-cam.location).to_track_quat('-Z','Y').to_euler()
    sc.render.filepath=f"{out}_{nm}.png"; bpy.ops.render.render(write_still=True)
