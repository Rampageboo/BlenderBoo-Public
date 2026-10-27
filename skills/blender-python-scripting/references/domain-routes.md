# Blender API pitfalls by domain

A lookup map, not a sequence. Tool names and fields come from the live catalog.

| Domain | Check before trusting custom code |
| --- | --- |
| BMesh, modeling, modifiers | Edit Mode BMesh vs object mesh data; inspect evaluated geometry after modifiers. |
| Geometry Nodes | Read the live group interface and socket identifiers before linking or assigning values. |
| Shader nodes and images | Resolve live node/socket names; render the material, since valid links do not prove appearance. |
| Compositor | Confirm the node tree and render output in the intended engine and view layer. |
| Animation and rigging | Separate edit bones, pose bones, actions, and evaluated poses; inspect actual frames and deformation. |
| Physics and simulation | Check cache ownership and saved paths; bake long simulations in a separate headless process. |
| Scene and rendering | Render and capture through the toolkit's deferred or child-process tools. |
| Python automation and add-ons | Confirm context and version; an add-on reload can invalidate the bridge, so toolkit source changes are a separate repository task. |
