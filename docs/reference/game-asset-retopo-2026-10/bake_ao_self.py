import bpy, sys
a=sys.argv[sys.argv.index('--')+1:]; NAME=a[0]; RES=int(a[1]); OUT=a[2]
sc=bpy.context.scene; vl=bpy.context.view_layer
lo=bpy.data.objects[NAME+'_low']
for o in sc.objects:
    if o.type=='MESH' and o!=lo: o.hide_render=True; o.hide_set(True)
sc.render.engine='CYCLES'; p=bpy.context.preferences.addons['cycles'].preferences; p.compute_device_type='OPTIX'; p.get_devices()
for d in p.devices: d.use=d.type!='CPU'
sc.cycles.device='GPU'; sc.cycles.samples=128
lo.data.materials.clear(); m=bpy.data.materials.new("ao"); m.use_nodes=True; lo.data.materials.append(m)
im=bpy.data.images.new(f"T_{NAME}_AO",RES,RES); im.colorspace_settings.name='Non-Color'
n=m.node_tree.nodes.new('ShaderNodeTexImage'); n.image=im; m.node_tree.nodes.active=n
for o in vl.objects: o.select_set(False)
lo.select_set(True); vl.objects.active=lo
b=sc.render.bake; b.use_selected_to_active=False; b.margin=16; b.margin_type='EXTEND'
if sc.world is None: sc.world=bpy.data.worlds.new('W')
sc.world.light_settings.distance=0.15
bpy.ops.object.bake(type='AO')
im.filepath_raw=f"{OUT}/T_{NAME}_AO.png"; im.file_format='PNG'; im.save(); print("AO done")
