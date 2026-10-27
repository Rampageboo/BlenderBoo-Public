# Product Intent

Build a stronger MCP layer that gives an agent reliable access to and control of
Blender. **The product is the tooling**, not any particular 3D asset: typed,
testable, high-level tools plus unrestricted `python.exec` for scene
inspection, mutation, validation, checkpoint and rollback, and visual review.

## The assets are test beds

The dense hemispherical shrub and the ROG Strix G16 product model exist to
exercise the tools against realistic scenes. They exposed what driving Blender
through unobserved `bpy` scripts cannot do — preview, test, reuse, or roll
back. `python.exec` is now supported with change evidence and checkpoints. Do
not mistake work on those scenes for the deliverable.

## Coverage target

Reliable coverage of most real workflows through high-level typed tools:
scene and object management, modeling, curves, modifiers, Geometry Nodes, UV,
look development, cameras, lights, world, compositing, import/export, rigging,
and animation.

This is deliberately **not** a one-to-one wrapper around every moving `bpy` or
third-party function. A typed tool exists when a reusable contract is useful;
otherwise the agent uses `python.exec` with state and verification discipline.

## Sequencing

Modeling is the highest priority; general polygon and hard-surface capability
comes before expanding the production-rendering track. Existing safe
render/preview tools stay, but new rendering work is sequenced after the
modeling core. The Owner explicitly deprioritized retopology.

## Owner working style

The Owner supplies intent and visual guidance — screenshots and Blender Empty
markers. A marker is an **example of a systemic rule**, not necessarily an
isolated defect to patch. Generalize it, preview the systemic fix, save a new
version, and render evidence.
