"""Pure geometry checks for the bounded hard-surface constructor subset."""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "addon"))
from blenderboo.errors import InvalidParamsError
from blenderboo.tools.hard_surface_build import (
    _Unsupported, _fillet_profile, _fit_basis, _lathe_geometry, _plate_grid,
    _triangle_gap_certificate, _outside_closed_surface,
    _robust_fit, _slab_geometry, _sweep_geometry, _thicken, _topology,
    build_plate, build_slab, clear_of, fair_fit,
)


class HardSurfaceBuildTests(unittest.TestCase):
    def test_slab_through_opening_and_blind_pocket_are_closed_quads(self):
        outer = [[-2,-1],[2,-1],[2,1],[-2,1]]
        opening = [[-.5,-.4],[.5,-.4],[.5,.4],[-.5,.4]]
        for extra, expected in (({}, "solid"), ({"opening": opening}, "through"),
                                ({"opening": opening, "pocket_depth": .2}, "pocket")):
            with self.subTest(mode=expected):
                vertices, faces, regions, mode, _, _ = _slab_geometry(
                    {"outline": outer, "thickness": .5, **extra})
                self.assertEqual(mode, expected)
                self.assertEqual(len(regions), len(faces))
                self.assertTrue(_topology(vertices, faces, True)["closed_manifold"])
        with self.assertRaises(_Unsupported):
            _slab_geometry({"outline": outer, "opening": [[3,0],[4,0],[4,1],[3,1]],
                            "thickness": .5})
    def test_capped_lathe_is_all_quad_closed_and_preserves_sharp_rings(self):
        vertices, faces, closed, sharp = _lathe_geometry({"segments": 12,
            "profile": [{"radius": 1, "z": 0, "sharp": True}, {"radius": 1.5, "z": 2}, {"radius": 1, "z": 3}]})
        counts = _topology(vertices, faces, closed)
        self.assertTrue(counts["closed_manifold"])
        self.assertEqual(counts["faces"], 42)
        self.assertEqual(len(sharp), 36)
        degrees = [0] * len(vertices)
        edges = {tuple(sorted((face[i], face[(i+1) % 4]))) for face in faces for i in range(4)}
        for a, b in edges:
            degrees[a] += 1
            degrees[b] += 1
        self.assertLessEqual(max(degrees), 4)
        self.assertEqual(sum(degree == 3 for degree in degrees), 8)
        with self.assertRaises(_Unsupported):
            _lathe_geometry({"segments": 7, "profile": [{"radius": 1, "z": 0}, {"radius": 1, "z": 1}]})

    def test_profile_fillet_is_tangent_circle_and_rejects_overlap(self):
        raw = [{"radius": 1, "z": 0}, {"radius": 1, "z": 1, "round_radius": .2}, {"radius": 2, "z": 1}]
        profile, _ = _fillet_profile(raw, False, 4)
        np.testing.assert_allclose(profile[1], [1, .8])
        np.testing.assert_allclose(profile[-2], [1.2, 1])
        np.testing.assert_allclose(np.linalg.norm(profile[1:-1] - [1.2, .8], axis=1), .2)
        raw[1]["round_radius"] = 2
        with self.assertRaises(_Unsupported):
            _fillet_profile(raw, False, 4)

    def test_closed_returning_lathe_profile_is_manifold(self):
        vertices, faces, closed, _ = _lathe_geometry({"closed_profile": True, "segments": 8,
            "profile": [{"radius": 1, "z": 0}, {"radius": 2, "z": 0}, {"radius": 2, "z": 1}, {"radius": 1, "z": 1}]})
        self.assertEqual(_topology(vertices, faces, closed)["boundary_edges"], 0)

    def test_chamfered_miter_sweep_and_closed_loop(self):
        path = [{"point": p, "width": .2, "height": .3, "chamfer": .03} for p in ([0,0,0], [2,0,0], [2,2,0])]
        vertices, faces = _sweep_geometry({"path": path})
        self.assertTrue(_topology(vertices, faces, True)["closed_manifold"])
        self.assertEqual(len(vertices), 24)
        loop = [{"point": p, "width": .2, "height": .3} for p in ([0,0,0], [2,0,0], [2,2,0], [0,2,0])]
        vertices, faces = _sweep_geometry({"path": loop, "closed": True})
        self.assertEqual(_topology(vertices, faces, True)["boundary_edges"], 0)
        loop[2]["point"] = [2,2,.2]
        with self.assertRaises(_Unsupported):
            _sweep_geometry({"path": loop, "closed": True})

    def test_sweep_rejects_collapsed_or_incompatible_sections(self):
        path = [{"point": [0,0,0], "width": 1, "height": 1}, {"point": [0,0,0], "width": 1, "height": 1}]
        with self.assertRaises(_Unsupported):
            _sweep_geometry({"path": path})
        path[1]["point"] = [1,0,0]
        path[1]["chamfer"] = .1
        with self.assertRaises(_Unsupported):
            _sweep_geometry({"path": path})

    def test_quad_plate_boundary_and_thickness(self):
        vertices, faces, normal = _plate_grid({"outline": [[0,0,0], [2,0,0], [3,2,0], [0,2,0]], "subdivisions": [3,2]})
        np.testing.assert_allclose(vertices[[0,3,11,8]], [[0,0,0],[2,0,0],[3,2,0],[0,2,0]])
        thick, shell = _thicken(vertices, faces, normal, .2, False)
        self.assertTrue(_topology(thick, shell, True)["closed_manifold"])
        opened, open_faces = _thicken(vertices, faces, normal, .2, True)
        self.assertGreater(_topology(opened, open_faces, False)["boundary_edges"], 0)
        result = build_plate({"name": "Unsupported", "outline": [[0,0,0],[2,0,0],[1,.5,0],[2,2,0],[0,2,0]]}).data
        self.assertFalse(result["changed"])
        self.assertEqual(result["status"], "unsupported")

    def test_convex_xy_polygons_have_conforming_all_quad_thickness(self):
        for count in (3,5,6):
            outline = [[float(np.cos(t)),float(np.sin(t))] for t in np.arange(count)*2*np.pi/count]
            vertices,faces,normal = _plate_grid({"outline":outline,"subdivisions":[2,2]})
            self.assertEqual(len(faces),count*3*4)
            self.assertTrue((abs(vertices[:,2]) < 1e-12).all())
            solid,closed = _thicken(vertices,faces,normal,.1,False)
            self.assertTrue(_topology(solid,closed,True)["closed_manifold"])
        with self.assertRaises(_Unsupported):
            _plate_grid({"outline":outline,"subdivisions":[2,3]})
        star = [[float(np.cos(t)),float(np.sin(t))] for t in np.array([0,2,4,1,3])*2*np.pi/5]
        with self.assertRaises(_Unsupported):
            _plate_grid({"outline":star})

    def test_continuous_gap_certificate_and_bounded_uncertainty(self):
        triangles = np.array([[[0,0,1],[2,0,1],[0,2,1]]],dtype=float)
        certificate = _triangle_gap_certificate(triangles,lambda p:abs(float(p[2])),[.8,1.2],1e-6)
        self.assertEqual(certificate["status"],"certified")
        self.assertGreaterEqual(certificate["minimum_scene_units"],.8)
        self.assertLessEqual(certificate["maximum_scene_units"],1.2)
        unresolved = _triangle_gap_certificate(triangles,lambda p:abs(float(p[2])),[.8,1.2],1e-6,max_cells=1)
        self.assertEqual(unresolved["status"],"unresolved")
        bad = _triangle_gap_certificate(triangles,lambda p:abs(float(p[2])),[1.1,1.2],1e-6)
        self.assertEqual(bad["status"],"violated")

    def test_solid_angle_outside_rejects_embedded_parts(self):
        points = np.array([[0,0,0],[1,0,0],[0,1,0],[0,0,1]],dtype=float)
        triangles = points[np.array([[0,2,1],[0,1,3],[0,3,2],[1,2,3]])]
        self.assertFalse(_outside_closed_surface(np.array([.1,.1,.1]),triangles))
        self.assertTrue(_outside_closed_surface(np.array([2,2,2]),triangles))

    def test_robust_fit_rejects_outliers_and_preserves_x_mirror(self):
        theta, phi = np.meshgrid(np.arange(48)*7.5, np.linspace(-60,75,18))
        basis = _fit_basis(theta, phi, 2, 2)
        coef = np.zeros(9)
        coef[0], coef[1], coef[6] = 2, .1, .05
        truth = basis @ coef
        observed = truth.copy()
        observed[2:4, 5:8] += .5
        found, fitted, keep = _robust_fit(basis, observed, 8, 2.5)
        self.assertLess(float(np.max(abs(fitted-truth))), 1e-5)
        self.assertLess(int(keep.sum()), observed.size)
        mirrored = _fit_basis(180-theta, phi, 2, 2) @ found
        np.testing.assert_allclose(fitted, mirrored, atol=1e-9)
        with self.assertRaises(_Unsupported):
            _robust_fit(basis, np.full(observed.shape, np.nan), 4, 2.5)

    def test_unsupported_options_do_not_require_blender_imports(self):
        self.assertEqual(clear_of({"delete_hidden": True}).data["status"], "unsupported")
        self.assertEqual(fair_fit({"source": "unused", "center": [0,0,0], "phi_range": [-60,90]}).data["status"], "unsupported")

    def test_folded_quad_rejected_before_blender_allocation(self):
        with self.assertRaises(_Unsupported):
            _topology([[0,0,0],[1,1,0],[1,0,0],[0,1,0]], [(0,1,2,3)], False)
        with self.assertRaises(InvalidParamsError):
            build_plate({"name": "No", "unhandled": True})


if __name__ == "__main__":
    unittest.main()
