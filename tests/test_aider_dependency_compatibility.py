"""Exercise Aider's real imports and numerical ranking on each tested Python."""

import networkx as nx
import pytest


def test_aider_backend_dependencies_import():
    from auto_coder.aider_client import Coder, InputOutput, Model

    # AiderClient catches ImportError, so assert the backend was actually loaded.
    assert Coder is not None
    assert InputOutput is not None
    assert Model is not None


def test_aider_repository_ranking_numerical_dependencies():
    # RepoMap uses NetworkX PageRank, which executes NumPy and SciPy sparse code.
    graph = nx.DiGraph([("caller", "callee"), ("callee", "callee")])
    ranking = nx.pagerank(graph, alpha=0.85)

    assert ranking == pytest.approx({"caller": 0.075, "callee": 0.925})
