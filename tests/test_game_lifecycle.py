"""Host-side regression tests for game loading and task shutdown."""
import ast
import asyncio
from pathlib import Path
import unittest
from unittest.mock import Mock, AsyncMock
from types import SimpleNamespace

SOFTWARE = Path(__file__).resolve().parents[1] / "software"


def load_method(path, name, namespace):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    method = next(node for node in ast.walk(tree)
                  if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and node.name == name)
    exec(compile(ast.Module(body=[method], type_ignores=[]),
                 str(path), "exec"), namespace)
    return namespace[name]


class GameLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_waits_for_tasks(self):
        for name in ("flappy", "pacman", "snake", "tetris", "pong", "tictactoe"):
            with self.subTest(game=name):
                stopped = []
                async def worker():
                    try:
                        await asyncio.Event().wait()
                    finally:
                        await asyncio.sleep(0)
                        stopped.append(True)

                game = Mock()
                game.running = True
                multiplayer = name in ("pong", "tictactoe")
                tasks = [asyncio.create_task(worker())
                         for _ in range(2 if multiplayer else 1)]
                deinit_counts = []
                uart = game.uart
                if multiplayer:
                    game._tasks = tasks
                    game.uart.deinit.side_effect = lambda: deinit_counts.append(len(stopped))
                else:
                    game._task = tasks[0]
                await asyncio.sleep(0)
                helper = load_method(SOFTWARE / "bsides.py", "cancel_game_task",
                                     {"asyncio": asyncio})
                stop = load_method(SOFTWARE / "games" / (name + ".py"),
                                   "_stop", {"asyncio": asyncio, "bsides": SimpleNamespace(cancel_game_task=helper)})
                await stop(game)
                self.assertFalse(game.running)
                self.assertTrue(all(task.done() for task in tasks))
                self.assertEqual(len(stopped), len(tasks))
                if multiplayer:
                    self.assertEqual(game._tasks, ())
                    uart.deinit.assert_called_once()
                    self.assertEqual(deinit_counts, [len(tasks)])
                else:
                    self.assertIsNone(game._task)


class GameRegistryTests(unittest.IsolatedAsyncioTestCase):
    def test_registry_matches_bundled_modules(self):
        tree = ast.parse((SOFTWARE / "bsides.py").read_text(encoding="utf-8"))
        registry = next(node for node in tree.body
                        if isinstance(node, ast.Assign)
                        and any(isinstance(t, ast.Name) and t.id == "GAMES"
                                for t in node.targets))
        games = ast.literal_eval(registry.value)
        self.assertIsInstance(games, tuple)
        modules = [module for _, module in games]
        self.assertEqual(modules, sorted(p.stem for p in
                         (SOFTWARE / "games").glob("*.py")
                         if not p.name.startswith("_")))
        self.assertEqual(len(modules), len(set(modules)))


    async def test_menu_defers_import_until_selection(self):
        path = SOFTWARE / "bsides.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        nodes = [node for node in tree.body
                 if (isinstance(node, ast.Assign)
                     and any(isinstance(t, ast.Name) and t.id == "GAMES"
                             for t in node.targets))
                 or (isinstance(node, ast.ClassDef)
                     and node.name == "GamesScreen")]

        class ListScreen:
            def __init__(self, oled, title, items):
                self.oled = oled
                self.items = items
                self.headerwriter = Mock()

        oled = Mock()
        game = Mock()
        game.GameScreen.return_value = object()
        calls = []

        def import_game(name, *args):
            calls.append(name)
            self.assertEqual(oled.show.call_count, 1)
            return game

        namespace = {"ListScreen": ListScreen, "__import__": import_game}
        exec(compile(ast.Module(body=nodes, type_ignores=[]),
                     str(path), "exec"), namespace)
        screen = namespace["GamesScreen"](oled)
        self.assertEqual(calls, [])
        self.assertIs(screen.items, namespace["GAMES"])
        self.assertIs(await screen.on_select(3), game.GameScreen.return_value)
        self.assertEqual(calls, ["games.snake"])
        game.GameScreen.assert_called_once_with(oled)

        def broken_import(*args):
            raise ImportError("broken game")

        namespace["__import__"] = broken_import
        self.assertIs(await screen.on_select(0), screen)

        namespace["__import__"] = lambda *args: game
        game.GameScreen.return_value = SimpleNamespace(
            start=AsyncMock(side_effect=RuntimeError("startup failed")),
            _stop=AsyncMock())
        self.assertIs(await screen.on_select(0), screen)
        game.GameScreen.return_value._stop.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
