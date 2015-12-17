import minqlx

class test(minqlx.Plugin):
    def __init__(self):
        super().__init__()
        self.add_hook("player_loaded", self.handle_player_loaded)
        self.add_hook("game_start", self.handle_game_start)
        self.add_hook("game_end", self.handle_game_end)
        self.add_hook("chat", self.handle_chat)
        self.add_hook("round_start", self.handle_round_start)
        self.add_hook("round_end", self.handle_round_end)

    def handle_chat(self, player, msg, channel):
        self.console("chat")
    
    def handle_player_loaded(self, player):
        self.console("connect")

    def handle_game_start(self, game):
        self.console("start")

    def handle_game_end(self, data):
        self.console("end")
        
    def handle_round_start(self, *args, **kwargs):
        self.console("round_start")

    def handle_round_end(self, *args, **kwargs):
        self.console("round_end")
