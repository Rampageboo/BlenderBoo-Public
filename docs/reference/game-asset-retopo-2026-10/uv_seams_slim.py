import bpy, bmesh, sys
a=sys.argv[sys.argv.index('--')+1:]; NAME=a[0]; OUT=a[1]
lo=bpy.data.objects[NAME+'_low']; vl=bpy.context.view_layer
bm=bmesh.new(); bm.from_mesh(lo.data)
n=0
for e in bm.edges:
    v0,v1=e.verts
    if abs(v0.co.x)<1e-4 and abs(v1.co.x)<1e-4 and v0.co.y>0 and v1.co.y>0: e.seam=True; n+=1
print("back seam edges",n, "total seams",sum(1 for e in bm.edges if e.seam))
bm.to_mesh(lo.data); bm.free()
for o in vl.objects: o.select_set(False)
vl.objects.active=lo; lo.select_set(True)
bpy.ops.object.mode_set(mode='EDIT'); bpy.ops.mesh.select_all(action='SELECT')
bpy.ops.uv.unwrap(method='MINIMUM_STRETCH', margin=0.004)
bpy.ops.uv.average_islands_scale(); bpy.ops.uv.pack_islands(rotate=True, margin=0.004)
bpy.ops.object.mode_set(mode='OBJECT')
if not any(m.type=='TRIANGULATE' for m in lo.modifiers):
    t=lo.modifiers.new("Triangulate",'TRIANGULATE'); t.quad_method='BEAUTY'; t.ngon_method='BEAUTY'; t.min_vertices=4
bm=bmesh.new(); bm.from_mesh(lo.data); uv=bm.loops.layers.uv.active
def ar(f):
    p=[l[uv].uv for l in f.loops]; return abs(sum(p[i].x*p[(i+1)%len(p)].y-p[(i+1)%len(p)].x*p[i].y for i in range(len(p))))/2
# islands count + stretch: ratio uv-area/3d-area spread
import statistics
r=[ar(f)/max(f.calc_area(),1e-12) for f in bm.faces]; med=statistics.median(r)
print("UV coverage",round(sum(ar(f) for f in bm.faces),4),"zero",sum(1 for f in bm.faces if ar(f)<1e-9),"density p5/p95 vs median",round(sorted(r)[len(r)//20]/med,2),round(sorted(r)[len(r)*19//20]/med,2))
bpy.ops.wm.save_as_mainfile(filepath=OUT,copy=True)
