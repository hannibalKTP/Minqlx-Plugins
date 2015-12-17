import minqlx

class test(minqlx.Plugin):
    def __init__(self):
        super().__init__()
        self.add_hook("player_loaded", self.handle_player_loaded)
        self.add_hook("game_start", self.handle_game_start)
        self.add_hook("game_end", self.handle_game_end)
    
    def handle_player_connect(self, player):
        self.console("connect")

    def handle_game_start(self, game):
        self.console("start")

    def handle_game_end(self, data):
        self.console("end")

