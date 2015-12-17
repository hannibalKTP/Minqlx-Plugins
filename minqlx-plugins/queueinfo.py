# Copyright (C) WalkerY (github) aka WalkerX (ql)

# This file is part of minqlbot.

# minqlbot is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# minqlbot is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with minqlbot. If not, see <http://www.gnu.org/licenses/>.

"""Displays Queue of players and also detects AFKs/Not playing players.
Players are removed from queue after they have played for PENDING_REMOVAL_TIME
seconds (defaults to 4 minutes) or after they have been disconnected for 
ABSENT_REMOVAL_TIME seconds (defaults to 2 minutes).
When they are in teams and in queue they are marked green. Whey they are disconnected
and still in queue they are not displayed but their waiting time is preserved for
ABSENT_REMOVAL_TIME seconds.

NOTE !!!! Plugin assumes that you are not playing on bot account and bot account
is never shown in queue. This may be configurable in future releases.

Setrule command allows one to introduce non-standard playing order rule so
that it is displayed when players connect and also when they use !queue
command. This is partially introduced for compatibility with 'top' plugin.

Summary of features:
- players don't loose their queue reservation if they join teams but don't play
    (warmup doesn't count as playing), min. of 4 minutes of playing required 
for player to loose his queue reservation.
- players don't loose their queue reservation if they disconnect but reconnect
soon enough (defaults to 2 min max absent time)
- players don't change their queue status if they are switched during balance
- players are marked AFK/Not playing if they are waiting for a specified longer
time and are not displayed in main queue order. They can mark themselves as playing
by using !here command or its aliases !back, !waiting, !playing
- players that are banned from joning (flagged) by balance or ban plugin are
not displayed in queue
- players switching back from AFK/Not playing status don't loose their reservation
and waiting time is not reset, if you don't like their afking - kick them.
- bot account is never displayed in queue, its not yet configurable
- queueinfo plugin shares afk/not playing info with 'top' plugin

Config sample:
    [QueueInfo]

    # Automatically mark player as Not Playing if he is waiting specified
    # time in minutes in queue on spec and not joining. He can mark himself 
    # as WAITING again by using !here command or one of its aliases. Value
    # 0 disables this feature (players are not marked afk automatically).
    WaitingTime: 20

"""


import minqlx
import datetime
import time
import string
import re
import threading

class queueinfo(minqlx.Plugin):

    # Interface class for queueinfo plugin.
    # This interface is not thread-safe.
    class QueueInfoInterface:
        __NAME = "queueinfo"
        
        def __init__(self, queue_plugin):
            self._queue_plugin = queue_plugin
            if queue_plugin is None:
                raise RuntimeError("Invalid constructor argument.")
            
        @property
        def _plugin(self):
            if self.is_loaded():
                return self._queue_plugin
            else:
                raise RuntimeError("{} plugin not loaded but requested.".format(self.__NAME))
            
        def is_loaded(self):
            return (self.__NAME in self._queue_plugin.plugins and
                    self._queue_plugin.plugins[self.__NAME] is self._queue_plugin)
                
        @property
        def not_playing_players(self):
            '''We return list of players marked afk/not playing and flagged
            by external data soruces - banned from playing (ban, balance 
            plugins).
            
            '''
            list = []
            queue = self._plugin.queue
            #self._plugin.update_and_remove_banned_from_joining()
            
            for name in queue:
                self._plugin.try_set_notplaying(name)
                if self._plugin.is_notplaying(name):
                    pl = self._plugin.player(name)
                    if pl is not None:
                        list.append(pl)

            with self._plugin.players_to_remove_lock:
                for player_name in self._plugin.players_to_remove:
                    pl = self._plugin.players_to_remove[player_name]
                    if pl not in list:
                        list.append(pl)

            return list
            
        @property
        def full_rule_str(self):
            return self._plugin.get_rule_str()
            
        @property    
        def rule_str(self):
            return self._plugin.rule
            
        @property
        def rule_time(self):
            if self._plugin.rule_time is None:
                return None
            return self._plugin.rule_time
            
        @property    
        def version(self):
            return self._plugin.__version__


    def __init__(self):
        super().__init__()
        self.__version__ = "0.13.4"
        self.add_hook("player_connect", self.handle_player_connect)
        self.add_hook("player_loaded", self.handle_player_connect)
        self.add_hook("player_disconnect", self.handle_player_disconnect)
        self.add_hook("team_switch", self.handle_team_switch)
        self.add_hook("round_start", self.handle_round_start, priority=minqlx.PRI_LOW)
        self.add_hook("round_end", self.handle_round_end, priority=minqlx.PRI_LOW)        
        #self.add_hook("bot_disconnect", self.handle_bot_disconnect)
        #self.add_hook("bot_connect", self.handle_bot_connect)                
        self.add_command(("queue", "kolejka", "q", "k"), self.cmd_queue, 0)
        self.add_command(("playing", "here", "notafk", "waiting"), self.cmd_playing, 0)
        self.add_command(("notplaying", "afk", "gone", "bye", "brb", "bb"), self.cmd_notplaying, 0)
        self.add_command("setrule", self.cmd_setrule, 5, usage="<time> <rule> (time=HH:MM)")
        self.add_command("remrule", self.cmd_remrule, 5)
        self.add_command("version", self.cmd_version, 0)

        # Minimum play time before removal from queue in seconds.
        # Warmup in team doesn't count as play time.
        self.PENDING_REMOVAL_TIME = 240
        
        # Minimum absent time after leaving server before removal from
        # queue in seconds.
        self.ABSENT_REMOVAL_TIME = 120
        
        # Should those players be displayed in black after disconnecting
        # or not displayed at all?
        self.ABSENT_PENDING_REMOVAL_DISPLAY = False
        
        # For compatibility with 'balance' command, if player speced only 
        # for this amount of seconds we treat him as he never joined queue
        # if he returns to teams soon enough
        self.IMMEDIATE_REJOIN_MAX_SPEC_TIME = 4
        
        # List of players that need to be removed from queue based
        # on external data sources (like other plugins)
        # Lock for future thread-safety of queueinfo plugin.
        self.players_to_remove = {}
        self.players_to_remove_lock = threading.RLock()
        
        # List of players in queue, their queue join times and other info as required.
        # {"walkerx": {"joinTime": datetime}}
        self.queue = {}
                
        self.initialize()
        
        self.interface = queueinfo.QueueInfoInterface(self)

        self.rule = ""
        self.rule_time = None
        self.standard_template = "^6[RULE]^7 after ^6[TIME]^7 BOT local time. Try ^6!time^7."
        self.time_re = re.compile(r'^(([01]\d|2[0-3]):([0-5]\d)|24:00)$')
        
    def initialize(self):
        self.queue = {}
        specs = self.teams()["spectator"]
        for spec in specs:
            name = spec.clean_name.lower()
            if name not in self.queue:
                self.add(spec)
                time.sleep(0.01) # let's make each time differ always internally
        #self.remove_bot()
    
    def handle_bot_connect(self):
        self.initialize()
    
    def handle_bot_disconnect(self):
        pass
    def handle_player_connect(self, player):
        name = player.clean_name.lower()
        if not name:
            return
        self.try_removal(name)
        if name not in self.queue:
            self.add(player)
        else:
            # Quickly returning player - we put him back into
            # present status in queue.
            if name in self.queue:
                if "disconnectTime" in self.queue[name]:
                    del self.queue[name]["disconnectTime"]
                    self.queue[name]["player"] = player

        #self.remove_bot()
        
        if self.rule != "":
            self.delay(20, lambda: player.tell("^1WARNING ^7!!! Custom order rule: ^6{}".format(self.rule)))
        
    def handle_player_disconnect(self, player, info):
        name = player.clean_name.lower()
        
        # Remove afk info
        self.mark_playing(name)
        self.try_removal(name)
        if name in self.queue:
            if "pendingRemoval" in self.queue[name]:
                del self.queue[name]["pendingRemoval"]
            if "pendingRemovalTime" in self.queue[name]:
                del self.queue[name]["pendingRemovalTime"]
            self.queue[name]["disconnectTime"] = datetime.datetime.now()
            # player instance not valid anymore
            del self.queue[name]["player"]
            
    def handle_team_switch(self, player, old_team, new_team):
        name = player.clean_name.lower()
        if new_team == "spectator":
            if name not in self.queue:
                self.add(player, origin = old_team)
            else:
                self.cancel_pending_remove(name)
        elif new_team != "spectator":
            if name in self.queue:
                # Remove afk info as player joins
                # teams
                self.mark_playing(name)
                
                # We check if player speced just for
                # a few seconds originating from teams
                # and if so we remove him from queue
                # without pending remove
                if ("origin" in self.queue[name] and 
                    self.queue[name]["origin"] in ["red", "blue"]):
                    td = datetime.datetime.now() - self.queue[name]["joinTime"]
                    
                    # If he didn't rejoin immediately.
                    if (td.days > 0 or 
                        td.seconds >= self.IMMEDIATE_REJOIN_MAX_SPEC_TIME):
                            del self.queue[name]["origin"]
                            self.pending_remove(name)
                    else:    
                        del self.queue[name]
                else:
                    self.pending_remove(name)
                
        #self.remove_bot()

    def handle_round_end(self, score):        
        self.try_removals()

    def handle_round_start(self, round_):
        # Start counting playtime for all players
        # that have been in queue
        for name in self.queue:
            if ("player" in self.queue[name] and
                self.queue[name]["player"].team != "spectator" and
                "pendingRemoval" in self.queue[name] and
                self.queue[name]["pendingRemoval"] and
               "pendingRemovalTime" not in self.queue[name]):
                self.queue[name]["pendingRemovalTime"] = \
                    datetime.datetime.now()

    def cmd_version(self, player, msg, channel):    
        channel.reply("^6QueueInfo^7 plugin version ^6{}^7, author: ^6WalkerY^7 (github)".format(self.__version__))
                    
    def cmd_remrule(self, player, msg, channel):    
        self.rule = ""
        self.rule_time = None
        self.msg("^7Queue rule removed. Normal playing order now.")

    def cmd_setrule(self, player, msg, channel):    
        if len(msg) < 2:
            return minqlx.RET_USAGE
        
        is_time = False
        if len(msg) > 2:
            time_suspected = msg[1].strip()
            if time_suspected[0] in string.digits:
                is_time = bool(self.time_re.match(time_suspected))
                if not is_time:
                    return minqlx.RET_USAGE
                    
        if is_time:
            hour = int(time_suspected[:2])
            minute = int(time_suspected[3:5])
           
        if is_time:
            self.rule = " ".join(msg[2:]).upper()
            self.rule_time = datetime.time(hour, minute)
        else:
            self.rule = " ".join(msg[1:]).upper()
            self.rule_time = None
   
        self.msg("^7Playing order rule: ^6{}^7.".format(self.get_rule_str()))
        
    def cmd_playing(self, player, msg, channel):    
        self.mark_playing(player.clean_name.lower())
        channel.reply("^7Player {} ^7was marked as playing.".format(player.name))
    
    def cmd_notplaying(self, player, msg, channel):    
        if player.team == "spectator":
            self.mark_notplaying(player.clean_name.lower())
            channel.reply("^7Player {} ^7was marked as not playing.".format(player.name))
        else: 
            channel.reply("^7Player {} ^7can't be marked as not playing.".format(player.name))
    
    def cmd_queue(self, player, msg, channel):        
        maxwaitingtime = self.get_max_waiting_time()
        
        # those removals relate to players that
        # are banned from joining by other plugins
        #self.update_and_remove_banned_from_joining()
        
        # those relate to internal pending removals in queue
        self.try_removals()
            
        time_now = datetime.datetime.now()
        namesbytime = sorted(self.queue, key=lambda x: self.queue[x]["joinTime"])       
        if not len(namesbytime):
            channel.reply("^7No players in queue.")
        else:
            reply = "^7QUEUE: "
            counter = 0
            notplaying = []
            for name in namesbytime:
                self.try_set_notplaying(name)
                
                if self.is_notplaying(name):
                    # add to Not Playing queue
                    notplaying.append(name)
                    continue

                diff = time_now - self.queue[name]["joinTime"]
                seconds = diff.days * 3600 * 24
                seconds = seconds + diff.seconds
                minutes = seconds // 60
                waiting_time = ""
                    
                if minutes:
                    waiting_time = "^6{}m^7".format(minutes)
                else:
                    waiting_time = "^6{}s^7".format(seconds)
                    
                if self.is_pending_removal(name):
                    counter = counter + 1
                    if counter != 1:
                        reply = reply + ", "
                    waiting_time += "^2*^7"
                    reply = reply + "^2{} ^7{}".format(name, waiting_time)
                elif self.is_on_short_leave(name):
                    if self.ABSENT_PENDING_REMOVAL_DISPLAY:
                        counter = counter + 1
                        if counter != 1:
                            reply = reply + ", "
                        waiting_time += "^0*^7"
                        reply = reply + "^0{} ^7{}".format(name, waiting_time)
                else:
                    counter = counter + 1
                    if counter != 1:
                        reply = reply + ", "
                    reply = reply + "^7{} ^7{}".format(self.queue[name]["name"], waiting_time)
                
            if counter != 0: 
                channel.reply(reply)
            
            if len(notplaying):
                reply = "^5NOT PLAYING: "
                counter = 0
                for name in notplaying:
                    diff = time_now - self.queue[name]["joinTime"]
                    seconds = diff.days * 3600 * 24
                    seconds = seconds + diff.seconds
                    minutes = seconds // 60
                    waiting_time = ""

                    if minutes:
                        waiting_time = "^5{}m^7".format(minutes)
                    else:
                        waiting_time = "^5{}s^7".format(seconds)
                    
                    if self.is_pending_removal(name):
                        counter = counter + 1
                        if counter != 1:
                            reply = reply + ", "                    
                        waiting_time += "^2*^7"
                        reply = reply + "^5{} ^5{}".format(name, waiting_time)
                    elif self.is_on_short_leave(name):
                        if self.ABSENT_PENDING_REMOVAL_DISPLAY:
                            counter = counter + 1
                            if counter != 1:
                                reply = reply + ", "                                            
                            waiting_time += "^0*^7"
                            reply = reply + "^5{} ^5{}".format(name, waiting_time)
                    else:
                        counter = counter + 1
                        if counter != 1:
                            reply = reply + ", "                                        
                        reply = reply + "^5{} ^5{}".format(name, waiting_time)
                    
                channel.reply(reply)     
                for name in notplaying:
                    player_ = self.player(name)
                    if player_:
                        if "autoNotPlaying" in self.queue[name] and self.queue[name]["autoNotPlaying"]:                       
                            player_.tell("^7Due to a long waiting time, you have been automatically marked as NOT PLAYING.")
                        player_.tell("^7{} ^7to change your status to WAITING type ^6!here^7 in chat".format(player_.name))
                
        if self.rule != "":
            channel.reply("^1WARNING ^7!!! Custom order rule: ^6{}".format(self.get_rule_str()))
            
    def get_interface(self):
        return self.interface

    def get_rule_str(self):
        if self.rule_time is not None:
            hour = self.rule_time.hour
            minute = self.rule_time.minute
            hour_str = ""
            minute_str = ""
            if hour < 10:
                hour_str = "0"
            if minute < 10:
                minute_str = "0"
            hour_str += "{}".format(hour)
            minute_str += "{}".format(minute)
            
            return self.standard_template.replace("[RULE]", self.rule.upper()).replace("[TIME]", "{}:{}".format(hour_str, minute_str))
        else:
            return self.rule.upper()
       
    def get_max_waiting_time(self):
        #config = minqlbot.get_config()
        # check for configured Auto Not Playing Time
        #if "QueueInfo" in config and "WaitingTime" in config["QueueInfo"]:
        #    maxwaitingtime = int(config["QueueInfo"]["WaitingTime"])
        #else:
        maxwaitingtime = 20
            
        return maxwaitingtime
        
    # check if marked as Not Playing
    def is_notplaying(self, name):
        if name in self.queue and "notPlaying" in self.queue[name]:
            return self.queue[name]["notPlaying"]
        else:
            return False

    def is_on_spec(self, name):
        pl = self.player(name)
        if pl is not None:
            if pl.team == "spectator":
                return True
            else:
                return False
        else:
            return False
    
    # Returns true if marking as not playing was 
    # required by the waiting time
    def try_set_notplaying(self, name):
        if not self.is_on_spec(name):
            return False
        maxwaitingtime = self.get_max_waiting_time()

        time_now = datetime.datetime.now()
        diff = time_now - self.queue[name]["joinTime"]
        seconds = diff.days * 3600 * 24
        seconds = seconds + diff.seconds
        minutes = seconds // 60     
        
        # Check if already marked as Not Playing
        if self.is_notplaying(name):
            return False
        # check if should be marked automatically as NotPlaying                    
        elif maxwaitingtime > 0 and minutes > maxwaitingtime:
            # check for time overrule
            if "playingOverrideTime" in self.queue[name]:
                diff2 = time_now - self.queue[name]["playingOverrideTime"]
                seconds2 = diff2.days * 3600 * 24
                seconds2 = seconds2 + diff2.seconds
                minutes2 = seconds2 // 60
                if minutes2 > maxwaitingtime:
                    self.mark_notplaying(name, True)
                    return True
            else:
                self.mark_notplaying(name, True)
                return True
            
    def add(self, player, origin = None):
        if not player.name:
            return
        logger = minqlx.get_logger()
        logger.debug("LOG TEST")
        name = player.clean_name.lower()
        logger.debug("Name0: {}".format(player.name))
        logger.debug("Name1: {}".format(player.clean_name))
        logger.debug("Name2: {}".format(name))
        self.queue[name] = {"joinTime": datetime.datetime.now(), 
                            "name": player.name,
                            "player": player}
        # Some additional info to remove player from queue
        # if he went spec for short time (during balance etc.)
        if origin is not None and origin in ["red", "blue"]:
            self.queue[name]["origin"] = origin
        
    def pending_remove(self, name):
        '''We don't remove immediately as player may not start playing
        after joining.  
        
        '''
        self.queue[name]["pendingRemoval"] = True

    def cancel_pending_remove(self, name):
        if name in self.queue:
            if "pendingRemoval" in self.queue[name]:
                del self.queue[name]["pendingRemoval"]
            if "pendingRemovalTime" in self.queue[name]:
                del self.queue[name]["pendingRemovalTime"]    
                
    def is_pending_removal(self, name):
        if name in self.queue:
            if "pendingRemoval" in self.queue[name]:
                return self.queue[name]["pendingRemoval"]
                
        return False 

    def is_on_short_leave(self, name):
        if name in self.queue:
            if "disconnectTime" in self.queue[name]:
                return True
        return False         
        
    def try_removals(self):
        for name in self.queue.copy():
            self.try_removal(name)
        
    def try_removal(self, name):
        '''We remove only if player played for some time or
        has been absent for specified time.  
        
        '''
        if name in self.queue:
            if "pendingRemovalTime" in self.queue[name]:
                delta = datetime.datetime.now() - \
                        self.queue[name]["pendingRemovalTime"]
                if (delta.days > 0 or 
                    delta.seconds > self.PENDING_REMOVAL_TIME):
                    del self.queue[name]
        if name in self.queue:                    
            if "disconnectTime" in self.queue[name]:
                delta = datetime.datetime.now() - \
                        self.queue[name]["disconnectTime"]
                if (delta.days > 0 or 
                    delta.seconds > self.ABSENT_REMOVAL_TIME):
                    del self.queue[name]
            
    # mark as not playing
    def mark_notplaying(self, name, automatic=False):
        if name in self.queue:
            self.queue[name]["notPlaying"] = True
            if "playingOverrideTime" in self.queue[name]:
                del self.queue[name]["playingOverrideTime"]
            if automatic:
                self.queue[name]["autoNotPlaying"] = True
    
    # mark as playing
    def mark_playing(self, name):
        if name in self.queue:
            if "notPlaying" in self.queue[name]:
                del self.queue[name]["notPlaying"]
            self.queue[name]["playingOverrideTime"] = datetime.datetime.now()
            if "autoNotPlaying" in self.queue[name]:
                del self.queue[name]["autoNotPlaying"]
            
    # remove bot from queue
    #def remove_bot(self):
    #    config = minqlbot.get_config()
    #    if "Core" in config and "Nickname" in config["Core"]:
    #        if config["Core"]["Nickname"].lower() in self.queue:
    #           del self.queue[config["Core"]["Nickname"].lower()]
                
    #def update_and_remove_banned_from_joining(self):
        #self.update_players_to_remove_list()
        #self.remove_found_to_be_banned_from_joining()
