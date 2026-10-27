import bpy, math, sys, numpy as np
from mathutils import Vector
from mathutils.bvhtree import BVHTree
OUT=sys.argv[sys.argv.index('--')+1]
C=Vector((0,0,0.55)); dg=bpy.context.evaluated_depsgraph_get()
lo=BVHTree.FromObject(bpy.data.objects['Helmet_low'],dg)
TH=np.arange(0,360,2.0); PH=np.arange(-60,90.1,2.0); R=np.full((len(PH),len(TH)),np.nan)
for i,ph in enumerate(PH):
    for j,th in enumerate(TH):
        d=Vector((math.cos(math.radians(ph))*math.cos(math.radians(th)),math.cos(math.radians(ph))*math.sin(math.radians(th)),math.sin(math.radians(ph))))
        h=lo.ray_cast(C+d*1.5,-d,1.5)
        if h[0]: R[i,j]=(h[0]-C).length
np.savez(OUT,TH=TH,PH=PH,R=R); print("RD done")
