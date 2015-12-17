# minqlbot - A Quake Live server administrator bot.
# Copyright (C) 2015 Mino <mino@minomino.org>

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


# This plugin is the result of starting with an initial idea, followed
# by a bunch of other ones, and then put together into one. If there's any
# plugin that needs to be rewritten, it's this huge mess right here.

"""Balancing plugin complete rewrite
"""

from threading import RLock
import minqlx
import random
import re
import threading
import datetime
import http.client
import json
import traceback


ELO_GAMETYPES = ("ca", "ffa", "ctf", "duel", "tdm")
_rating_key = "minqlx:players:{}:rating"
_rating_gametype_key = "minqlx:players:{}:rating:{}"
_quakelive_key = "minqlx:players:{}:old_quakelive_nick"

class balance(minqlx.Plugin):
    def __init__(self):
        self.add_hook("vote_called", self.handle_vote_called, priority=minqlx.PRI_HIGH)
        self.add_hook("vote_ended", self.handle_vote_ended)
        self.add_hook("player_connect", self.handle_player_connect)
        self.add_hook("player_disconnect", self.handle_player_disconnect)
        self.add_hook("player_loaded", self.handle_player_loaded)
        self.add_hook("team_switch", self.handle_team_switch)
        self.add_hook("round_countdown", self.handle_round_countdown)
        self.add_hook("game_end", self.handle_game_end)
        
        self.add_command(("teams", "teens"), self.cmd_teams)
        self.add_command("balance", self.cmd_balance, 1)
        self.add_command("do", self.cmd_do, 1)
        self.add_command(("agree", "a"), self.cmd_agree)
        self.add_command("setnickfor", self.cmd_setnickfor, 3, usage="<id> <nick> or <id> to delete")
        self.add_command(("ranknick","oldnick","nick","iam"), self.cmd_setnick, 0, usage="<nick>")
        self.add_command(("qlnick","getnick","shownick","whoami"), self.cmd_getnick, 0)
        self.add_command(("set_rating", "setelo"), self.cmd_set_rating, 3, usage="<id> <rating>")
        self.add_command(("getrating", "getelo", "elo"), self.cmd_getrating, 0 , usage="<id>")
        self.add_command(("allelo", "selo", "elos"), self.cmd_allelo)
        self.add_command(("remrating", "remelo"), self.cmd_remrating, 3, usage="<id>")
        
        self.add_command(("ratinginfo"), self.cmd_ratinginfo, 5)

        self.set_cvar_once("qlx_balance_vetounevenshuffle", "1")
        self.set_cvar_once("qlx_balance_autobalance", "1")
        self.set_cvar_once("qlx_balance_minimumrating", "0")
        self.set_cvar_once("qlx_balance_maximumrating", "0")
        self.set_cvar_once("qlx_balance_defaultrating", "1250")
        
        self.set_cvar_once("qlx_balance_allowspectators", "1")
        self.set_cvar_once("qlx_balance_minimumsuggestiondifference", "25")
        
        
        
        self.suggested_pair = None
        self.suggested_agree = [False, False]
        
        self.rlock = RLock()
        
        # Keys: DataGrabber().uid - Items: (DataGrabber(), nick_list, caller)
        self.lookups = {}
        # Rating Info in Memory key: steam_id items - (gametype, rating)
        self.rating = {}
        # Keys: nick - Items (steam_id, status, lookup-id, (gametype, rating))
        self.lookup_nicks = {}
        
        # All Players on Server Ready to receive Tells
        teams = self.teams()
        self.loaded_players = teams["red"] + teams["blue"] + teams["free"] + teams["spectator"]
        # We flag players who ought to be kickbanned, but since we delay it, we keep
        # a list of players who are flagged and prevent them from starting votes or joining.
        self.ban_flagged = []

        # A datetime.datetime instance of the point in time of the last round countdown.
        self.countdown = None
        
        self.vote = ""

    def handle_vote_called(self, caller, vote, args):
        self.info("handle_vote_called")
        if self.is_flagged(caller):
            return minqlx.RET_STOP_ALL
        self.vote = vote
        if vote == "shuffle":
            auto_reject = self.get_cvar("qlx_balance_vetounevenshuffle", bool)
            if auto_reject:
                teams = self.teams()
                if len(teams["red"] + teams["blue"]) % 2 == 1:
                    self.msg("^7Only call shuffle votes when the total number of players is an even number.")
                    return minqlx.RET_STOP_ALL

    @minqlx.delay(5)
    def handle_vote_ended(self, passed):
        self.info("handle_vote_ended")
        vote = self.vote
        if passed == True and vote == "shuffle":
            auto = self.get_cvar("qlx_balance_autobalance", bool)
            self.console(auto);
            if not auto:
                return
            else:
                teams = self.teams()
                total = len(teams["red"]) + len(teams["blue"])
                if total % 2 == 0:
                    self.average_balance(minqlx.CHAT_CHANNEL, self.game.type_short)
                else:
                    self.msg("^7I can't balance when the total number of players is not an even number.")

    def handle_team_switch(self, player, old_team, new_team):
        self.info("handle_team_switch, old:{} - new:{}".format(old_team, new_team))
        if new_team != "spectator":
            if self.is_flagged(player):
                player.tell("You don't meet the rating Requirement to join.")
                player.put("spectator")
                return
            else:
                gametype = self.game.type_short
                return self.check_rating_requirements(player, gametype, new_team, old_team)
        
    def handle_player_connect(self, player):
        self.info("handle_player_connect")
        gametype = self.game.type_short
        if not self.has_rating(player.steam_id, gametype):
            self.fetch_rating([player], gametype, (self.check_rating_requirements, (player, gametype)))
        else:
            self.check_rating_requirements(player, gametype)
            
    def handle_player_disconnect(self, player, reason):
        if player.steam_id in self.loaded_players:
            self.loaded_players.remove(player)

    @minqlx.delay(1)
    def handle_player_loaded(self, player):
        self.info("handle_player_loaded")
        gametype = self.game.type_short
        player.tell("This Server is ELO Managed.")
        self.loaded_players.append(player.steam_id)
        if not self.is_flagged(player):
            if player.clean_name in self.lookup_nicks:
                if self.lookup_nicks[player.clean_name][1] == "failed":
                    player.tell("^7Type '^6!iam ^2<Your-old-Quakelive-Nick>^7' to get a Rating.")

    def handle_round_countdown(self, round):
        self.info("handle_round_countdown")
        if self.suggested_agree[0] and self.suggested_agree[1]:
            self.execute_suggestion()
        
        self.countdown = datetime.datetime.now()

    def handle_game_end(self, game, score="", winner=""):
        self.info("handle_game_end")
        # Clear suggestion when the game ends to avoid weird behavior if a pending switch
        # is present and the players decide to do a rematch without doing !teams in-between.
        self.suggested_pair = None
        self.suggested_agree = [False, False]
        
        
       

    def cmd_ratinginfo(self, player, msg, channel):
        self.info("cmd_ratinginfo")
        gametype = self.game.type_short
        rating_key = _rating_gametype_key.format(player.steam_id, self.game.type_short)
        if rating_key in self.db:
            channel.reply("Your rating for {} in db: {}.".format(gametype, self.db[rating_key]))
        quakelive_key = _quakelive_key.format(player.steam_id)
        if quakelive_key in self.db:
            channel.reply("Your qlranks-nick in db: {}.".format(self.db[quakelive_key]))
        if self.has_rating(player.steam_id, self.game.type_short):
            channel.reply("Your rating for {} in memory: {}.".format(gametype, self.get_rating(player.steam_id, gametype)))
        if player.clean_name in self.lookup_nicks:
            channel.reply("Your lookup to qlranks for '{}' is {}.".format(player.clean_name, self.lookup_nicks[player.clean_name][1]))
    
    def cmd_allelo(self, player, msg, channel):
        teams = self.teams()
        gametype = self.game.type_short
        
        players = teams["red"] + teams["blue"] + teams["spectator"]
        for p in players:
            if not self.has_rating(p.steam_id, gametype):
                self.fetch_rating(players, gametype, (self.cmd_allelo, (player, msg, channel)))
                return
                
        red = ""
        blue = ""
        spec = ""
        for one in teams["red"]:
            red += "^2" + one.clean_name + " ^6" + str(self.get_rating(one.steam_id, gametype)) + "   "
        for one in teams["blue"]:
            blue += "^2" + one.clean_name + " ^6" + str(self.get_rating(one.steam_id, gametype)) + "   "
        for one in teams["spectator"]:
            spec += "^2" + one.clean_name + " ^6" + str(self.get_rating(one.steam_id, gametype)) + "   "
        self.msg("^7Elos:")
        self.msg("^1RED: "+red)
        self.msg("^4BLUE: "+blue)
        self.msg("^7SPEC: "+spec)
        return

    def cmd_setnick(self, player, msg, channel):
        self.info("cmd_setnick")
        if len(msg) < 2:
            return minqlx.RET_USAGE
        nick = msg[1]
        self.testnick(player, nick)

    def cmd_getnick(self, player, msg, channel):
        self.info("cmd_getnick")
        ret, theplayer = self.check_input(msg, player)
        if ret:
            return ret
        qlranks_key = _quakelive_key.format(theplayer.steam_id)
        nick = self.db[qlranks_key]
        channel.reply("{}'s quakelive-Nick is set to ^2{}^7.".format(theplayer.name, nick))

    def cmd_setnickfor(self, player, msg, channel):
        self.info("cmd_setnickfor")
        ret, theplayer = self.check_input(msg, player, 3, 2)
        if ret:
            return ret
        if len(msg) < 3:
            qlranks_key = _quakelive_key.format(theplayer.steam_id)
            del self.db[qlranks_key]
            self.remove_rating(theplayer.steam_id, self.game.type_short)
            channel.reply("{}'s quakelive-Nick has been removed.".format(theplayer.name))
            return
        else:
            nick = msg[2]
            self.testnick(theplayer, nick, True)
        return
        
    def cmd_getrating(self, player, msg, channel):
        self.info("cmd_getrating")
        ret, theplayer = self.check_input(msg, player)
        if ret:
            return ret
        self.report_rating([theplayer], channel)
    
    def cmd_remrating(self, player, msg, channel):
        self.info("cmd_remrating")
        ret, theplayer = self.check_input(msg, player)
        if ret:
            return ret
        rating_key = _rating_gametype_key.format(theplayer.steam_id, self.game.type_short)
        if not self.db[rating_key] and not self.has_rating(theplayer.steam_id, self.game.type_short):
            channel.reply("{}'s has no rating in {} yet.".format(theplayer.name, self.game.type))
        else:
            del self.db[rating_key]
            self.remove_rating(theplayer.steam_id, self.game.type_short)
            channel.reply("{}'s {} rating is removed.".format(theplayer.name, self.game.type))
        
    def cmd_set_rating(self, player, msg, channel):
        self.info("cmd_set_rating")
        ret, theplayer = self.check_input(msg, player, 3, 3)
        if ret:
            return ret
        try:
            i = int(msg[len(msg)-1])
        except ValueError:
            return minqlx.RET_USAGE
        rating_key = _rating_gametype_key.format(theplayer.steam_id, self.game.type_short)
        self.db[rating_key] = i
        self.remove_rating(theplayer.steam_id, self.game.type_short)
        channel.reply("{}'s {} rating is set to ^6{}^7.".format(player.name, self.game.type, i))
     
    def cmd_teams(self, player, msg, channel):
        """Displays the average ratings of each team, the difference between those values,
        as well as a switch suggestion that the bot determined would improve balance."""
        teams = self.teams()
        diff = len(teams["red"]) - len(teams["blue"])
        if not diff:
            self.teams_info(channel, self.game.type_short)
        else:
            channel.reply("^7Both teams should have the same number of players.")
        
    def cmd_balance(self, player, msg, channel):
        """Makes the bot switch players around in an attempt to create balanced teams based
        on ratings."""
        teams = self.teams()
        total = len(teams["red"]) + len(teams["blue"])
        if total % 2 == 0:
            self.average_balance(channel, self.game.type_short)
        else:
            channel.reply("^7I can't balance when the total number of players is not an even number.")

    def cmd_do(self, player, msg, channel):
        """Forces a suggested switch to be done."""
        if self.suggested_pair:
            self.execute_suggestion()

    def cmd_agree(self, player, msg, channel):
        """After the bot suggests a switch, players in question can use this to agree to the switch."""
        if self.suggested_pair and not (self.suggested_agree[0] and self.suggested_agree[1]):
            if self.suggested_pair[0] == player:
                self.suggested_agree[0] = True
            elif self.suggested_pair[1] == player:
                self.suggested_agree[1] = True

            if self.suggested_agree[0] and self.suggested_agree[1]:
                # If the game's in progress and we're not in the round_countdown time window, wait for next round.
                if self.game.state == "in_progress" and self.countdown:
                    td = datetime.datetime.now() - self.countdown
                    if td.seconds > AGREE_WINDOW:
                        self.msg("^7The switch will be executed at the start of next round.")
                        return

                # Otherwise, switch right away.
                self.execute_suggestion()   

    def has_rating(self, steam_id, gametype):
        self.info("has_rating")
        if steam_id in self.rating:
            if gametype in self.rating[steam_id]:
                return True
        return False
        
    def set_rating(self, steam_id, gametype, rating):
        self.info("set_rating")
        if not steam_id in self.rating:
            self.rating[steam_id] = {}
        self.rating[steam_id][gametype] = int(rating)
        
    def get_rating(self, steam_id, gametype):
        self.info("get_rating")
        if steam_id in self.rating:
            if gametype in self.rating[steam_id]:
                return self.rating[steam_id][gametype]
        return self.get_cvar("qlx_balance_defaultrating")
        
    def remove_rating(self, steam_id, gametype):
        self.info("remove_rating")
        if steam_id in self.rating:
            if gametype in self.rating[steam_id]:
                del self.rating[steam_id][gametype]
        
    def report_rating(self, player_list, channel):
        self.info("report_rating")
        pending_players = []
        for player in player_list:
            if not self.has_rating(player.steam_id, self.game.type_short):
                pending_players.append(player.steam_id)
        
        # We have no players waiting for
        if not pending_players:
            for player in player_list:
                channel.reply("{}'s {} rating is set to ^6{}^7.".format(player.name, self.game.type, self.get_rating(player.steam_id, self.game.type_short)))
            return
        self.fetch_rating(player_list, self.game.type_short, (self.report_rating, (player_list, channel)))
        
    def fix_old_nick(self, name):
        self.info("fix_old_nick")
        #select the largest part when nick with spaces
        tmpname = ''.join(i for i in name if ord(i)<128)
        tmpname = tmpname.replace(".","")
        parts = tmpname.split(" ")
        newname = ''
        for part in parts:
            if len(part) > len(newname):
                newname = part
        if not newname:
            newname = tmpname
        return newname
            
    def fetch_rating(self, player_list, gametype, callback, retry = 0, old_nick = ""):
        self.info("fetch_rating")
        
        if retry: # in Retry, at least one name in list was not yet set, reget all players
            tmp = []
            for player in player_list:
                newplayer = player
                if not player.clean_name:
                    try:
                        newplayer = self.player(player.steam_id)
                    except:
                        newplayer = player
                tmp.append(newplayer)
            player_list = tmp
        
        def get_players_without_rating(self, player_list):
            players_without_rating = []
            for player in player_list:
                if not self.has_rating(player.steam_id, gametype):
                    players_without_rating.append(player)
            return players_without_rating

        players_without_rating = get_players_without_rating(self, player_list)
        # Check DB
        for player in players_without_rating:
            rating_key = _rating_gametype_key.format(player.steam_id, gametype)
            rating = self.db[rating_key]
            if rating:
                self.set_rating(player.steam_id, gametype, rating)
                
        players_without_rating = get_players_without_rating(self, player_list)
        # Check External Sources
        wait_for_name = False
        if players_without_rating:
            handled_players = []
            qlranks_names = {}
            for player in players_without_rating:
                if old_nick:
                    qlranks_name = old_nick
                else:
                    qlranks_key = _quakelive_key.format(player.steam_id)
                    qlranks_name = self.db[qlranks_key]
                if not qlranks_name:
                    cleanname = player.clean_name
                    if cleanname:
                        qlranks_name = cleanname
                    else: # clean_name not set yet, wait for a bit
                        if retry < 20:
                            wait_for_name = True
                            handled_players.append(player)
                        else: # no clean_name after 20 seconds, set default rating
                            self.set_rating(player.steam_id, gametype, self.get_cvar("qlx_balance_defaultrating"))
                if qlranks_name:
                    qlranks_name = self.fix_old_nick(qlranks_name)
                if qlranks_name and qlranks_name in self.lookup_nicks: # is in lookup or lookup finished
                    if self.lookup_nicks[qlranks_name][0] != player.steam_id: # trying to look up nick for another steam_id, warning using default
                        self.msg("^2{}^7 already in use. Setting '^2{}^7' to the default Rating of ^6{}^7.".format(qlranks_name, player.clean_name, self.get_cvar("qlx_balance_defaultrating")))
                        self.set_rating(player.steam_id, gametype, self.get_cvar("qlx_balance_defaultrating"))
                    elif self.lookup_nicks[qlranks_name][1] == "failed": # lookup already failed, so set default rating
                        self.set_rating(player.steam_id, gametype, self.get_cvar("qlx_balance_defaultrating"))
                    elif self.lookup_nicks[qlranks_name][1] == "found": # lookup already found rating, set it
                        if gametype in self.lookup_nicks[qlranks_name][3]:
                            rank_rating = self.lookup_nicks[qlranks_name][3][gametype]
                        else:
                            rank_rating = self.get_cvar("qlx_balance_defaultrating")
                        self.set_rating(player.steam_id, gametype, rank_rating)
                    else: # player is in lookup as pending
                        handled_players.append(player)
                elif qlranks_name: # new name to look up
                    qlranks_names[player.steam_id] = qlranks_name
                    handled_players.append(player)
                else:
                    if not wait_for_name:
                        self.set_rating(player.steam_id, gametype, self.get_cvar("qlx_balance_defaultrating"))
                    
            if wait_for_name:
                self.fetch_rating_delayed(player_list, gametype, callback, retry)
            
            if len(qlranks_names):
                lookup = DataGrabber(self, qlranks_names)
                all_names = ""
                for steam_id, name in qlranks_names.items():
                    self.lookup_nicks[name] = [steam_id, "pending", lookup.uid, {}]
                    all_names += name + ","
                self.console("searching "+all_names)
                self.lookups[lookup.uid] = (lookup, player_list, gametype, callback)
                lookup.start()
        
        players_without_rating = get_players_without_rating(self, player_list)
        if players_without_rating:
            # Check for Bugs
            for test in players_without_rating:
                if test not in handled_players:
                    self.msg("^2{}^7 fell through all Nets.".format(test.clean_name))
                    return
            # all players without rating will be handled and fetch_rating will be recalled
        else: #no unhandled players left, call callback and be done
            if callback:
                callback[0](*callback[1])
            
    @minqlx.delay(1)
    def fetch_rating_delayed(self, player_list, gametype, callback, retry):
        self.info("fetch_rating_delayed")
        return self.fetch_rating(player_list, gametype, callback, retry+1)
        
    @minqlx.next_frame
    def fetch_rating_datagrabber(self, response, datagrabber):
        self.info("fetch_rating_datagrabber")
        lookup = self.lookups[datagrabber.uid]
        if not lookup:
            self.msg("Lookup not found.")
            return
        condensed_data = {}
        if datagrabber.status == 200 and response:
            for qlName in response["players"]:
                if not "nick" in qlName:
                    continue
                nick = qlName["nick"]
                ratings = {}
                for gametype in ELO_GAMETYPES:
                    if qlName[gametype]:
                        if qlName[gametype]["rank"]:
                            ratings[gametype] = qlName[gametype]["elo"]
                if ratings:
                    condensed_data[nick] = ratings
        gametype_now = lookup[2]
        #self.lookup_nicks: [steam_id:status:time:(gametype:elo)]
        for name, ratings in condensed_data.items():
            if name in self.lookup_nicks:
                steam_id = self.lookup_nicks[name][0]
                self.lookup_nicks[name][1] = "found"
                # set the nick in database and report
                """player = self.player(steam_id) 
                if player:
                    self.setnick(player, name)"""
                for gametype, rating in condensed_data[name].items():
                    self.lookup_nicks[name][3][gametype] = rating
                    if gametype == gametype_now:
                        self.set_rating(steam_id, gametype, rating)

            else:
                self.lookup_nicks[name] = [0, "failed", datagrabber.uid, {}]
                self.msg("^2{}^7 has no QLRanks.com - Rating for ^6{}^7.".format(name, gametype_now))
        for name, data in self.lookup_nicks.items():
            if data[1] == "pending" and data[2] == datagrabber.uid:
                self.lookup_nicks[name][1] = "failed"
                self.msg("^2{}^7 has no QLRanks.com - Rating for ^6{}^7.".format(name, gametype_now))
        del self.lookups[datagrabber.uid]
        
        return self.fetch_rating(lookup[1], lookup[2], lookup[3])

    def teams_info(self, channel, game_type):
        self.info("teams_info")
        """Send average team ratings and an improvement suggestion to whoever asked for it.
        """
        teams = self.teams()
        diff = len(teams["red"]) - len(teams["blue"])
        if diff:
            channel.reply("^7Both teams should have the same number of players.")
            return True
        
        players = teams["red"] + teams["blue"]
        for player in players:
            if not self.has_rating(player.steam_id, game_type):
                self.console("need all elo for teams")
                self.fetch_rating(players, game_type, (self.teams_info, (channel, game_type)))
                return

        self.console("have all elo for teams")


        avg_red = self.team_average(teams["red"], game_type)
        avg_blue = self.team_average(teams["blue"], game_type)
        switch = self.suggest_switch(teams, game_type)
        diff = len(teams["red"]) - len(teams["blue"])
        diff_rounded = abs(round(avg_red) - round(avg_blue)) # Round individual averages.
        if round(avg_red) > round(avg_blue):
            channel.reply("^1{} ^7vs ^4{}^7 - DIFFERENCE: ^1{}"
                .format(round(avg_red), round(avg_blue), diff_rounded))
        elif round(avg_red) < round(avg_blue):
            channel.reply("^1{} ^7vs ^4{}^7 - DIFFERENCE: ^4{}"
                .format(round(avg_red), round(avg_blue), diff_rounded))
        else:
            channel.reply("^1{} ^7vs ^4{}^7 - Holy shit!"
                .format(round(avg_red), round(avg_blue)))

        minimum_suggestion_diff = self.get_cvar("qlx_balance_minimumsuggestiondifference", int)

        if switch and switch[1] >= minimum_suggestion_diff:
            channel.reply("^7SUGGESTION: switch ^6{}^7 with ^6{}^7. Type !a to agree."
                .format(switch[0][0].clean_name, switch[0][1].clean_name))
            if not self.suggested_pair or self.suggested_pair[0] != switch[0][0] or self.suggested_pair[1] != switch[0][1]:
                self.suggested_pair = (switch[0][0], switch[0][1])
                self.suggested_agree = [False, False]
        else:
            i = random.randint(0, 99)
            if not i:
                channel.reply("^7Teens look ^6good!")
            else:
                channel.reply("^7Teams look good!")
            self.suggested_pair = None

        return True       
        
    def team_average(self, team, game_type):
        self.info("team_average")
        """Calculates the average rating of a team."""
        avg = 0
        
        if team:
            for p in team:
                avg += self.get_rating(p.steam_id,game_type)
            avg /= len(team)

        return avg     
        
    def average_balance(self, channel, game_type):
        self.console("average_balance")
        """Balance teams based on average team ratings.

        """
        teams = self.teams()
        total = len(teams["red"]) + len(teams["blue"])
        if total % 2 == 1:
            channel.reply("^7I can't balance when the total number of players isn't an even number.")
            return True

        players = teams["red"] + teams["blue"]
        for player in players:
            if not self.has_rating(player.steam_id, game_type):
                self.console("need all elo for balance")
                self.fetch_rating(players, game_type, (self.average_balance, (channel, game_type)))
                return

        self.console("have all elo for balance")
        
        # Start out by evening out the number of players on each team.
        diff = len(teams["red"]) - len(teams["blue"])
        if abs(diff) > 1:
            channel.reply("^7Evening teams...")
            if diff > 0:
                for i in range(diff - 1):
                    p = teams["red"].pop()
                    self.put(p, "blue")
                    teams["blue"].append(p)
            elif diff < 0:
                for i in range(abs(diff) - 1):
                    p = teams["blue"].pop()
                    self.put(p, "red")
                    teams["red"].append(p)

        # Start shuffling by looping through our suggestion function until
        # there are no more switches that can be done to improve teams.
        switch = self.suggest_switch(teams, game_type)
        if switch:
            self.msg("^7Balancing teams...")
            self.lock("red")
            self.lock("blue")
            while switch:
                p1 = switch[0][0]
                p2 = switch[0][1]
                self.msg("^7{} ^6<=> ^7{}".format(p1, p2))
                self.switch(p1, p2)
                teams["blue"].append(p1)
                teams["red"].append(p2)
                teams["blue"].remove(p2)
                teams["red"].remove(p1)
                teams["red"].remove(p1)
                switch = self.suggest_switch(teams, game_type)
            self.unlock("red")
            self.unlock("blue")
            avg_red = self.team_average(teams["red"], game_type)
            avg_blue = self.team_average(teams["blue"], game_type)
            diff_rounded = abs(round(avg_red) - round(avg_blue)) # Round individual averages.
            if round(avg_red) > round(avg_blue):
                self.msg("^7Done! ^1{} ^7vs ^4{}^7 - DIFFERENCE: ^1{}"
                    .format(round(avg_red), round(avg_blue), diff_rounded))
            elif round(avg_red) < round(avg_blue):
                self.msg("^7Done! ^1{} ^7vs ^4{}^7 - DIFFERENCE: ^4{}"
                    .format(round(avg_red), round(avg_blue), diff_rounded))
            else:
                self.msg("^7Done! ^1{} ^7vs ^4{}^7 - Holy shit!"
                    .format(round(avg_red), round(avg_blue)))
        else:
            channel.reply("^7Teams are good! Nothing to balance.")
        return True
            
    def suggest_switch(self, teams, game_type):
        self.info("suggest_switch")
        """Suggest a switch based on average team ratings.

        """
        avg_red = self.team_average(teams["red"], game_type)
        avg_blue = self.team_average(teams["blue"], game_type)
        cur_diff = abs(avg_red - avg_blue)
        min_diff = 999999
        best_pair = None

        for red_p in teams["red"]:
            for blue_p in teams["blue"]:
                r = teams["red"].copy()
                b = teams["blue"].copy()
                b.append(red_p)
                r.remove(red_p)
                r.append(blue_p)
                b.remove(blue_p)
                avg_red = self.team_average(r, game_type)
                avg_blue = self.team_average(b, game_type)
                diff = abs(avg_red - avg_blue)
                if diff < min_diff:
                    min_diff = diff
                    best_pair = (red_p, blue_p)

        if min_diff < cur_diff:
            return (best_pair, cur_diff - min_diff)
        else:
            return None       
                
    def execute_suggestion(self):
        self.info("execute_suggestion")
        self.switch(self.suggested_pair[0], self.suggested_pair[1])
        self.suggested_pair = None
        self.suggested_agree = [False, False]

    def flag_player(self, player):
        self.info("flag_player")
        if player not in self.ban_flagged:
            self.ban_flagged.append(player)
    
    def unflag_player(self, player):
        self.info("unflag_player")
        if player in self.ban_flagged:
            self.ban_flagged.remove(player)

    def is_flagged(self, player):
        self.info("is_flagged")
        return player in self.ban_flagged        
        
    def check_rating_requirements(self, player, game_type, new_team="spectator", old_team=""):
        self.info("check_rating_requirements")
        """Checks if someone meets the rating requirements to play on the server."""
        min_rating = self.get_cvar("qlx_balance_minimumrating", int)
        max_rating = self.get_cvar("qlx_balance_maximumrating", int)

        if not min_rating and not max_rating:
            return True

        if not self.has_rating(player.steam_id, game_type):
            rating = 0
        else:
            rating = self.get_rating(player.steam_id, game_type)
            
        if (rating > max_rating and max_rating != 0) or (rating < min_rating and min_rating != 0):
            allow_spec = self.get_cvar("qlx_balance_allowspectators", bool)
            if allow_spec or rating == 0:
                if not player:
                    return True
                if new_team != "spectator":
                    player.tell("You don't meet the rating Requirement to join.")
                    player.put("spectator")
                    #self.put(name, "spectator")
                    if rating > max_rating and max_rating != 0:
                        player.tell("^7Sorry, but you can have at most ^6{}^7 rating to play here and you have ^6{}^7.".format(max_rating, rating))
                    elif rating < min_rating and min_rating != 0 and rating != 0:
                        player.tell("^7Sorry, but you need at least ^6{}^7 rating to play here and you have ^6{}^7.".format(min_rating, rating))
                    elif rating < min_rating and min_rating != 0 and rating == 0:
                        self.tell_spec(player)
                    return True
                
            else:
                if not player:
                    return True
                if new_team != "spectator":
                    player.tell("You don't meet the rating Requirement to join.")
                    player.put("spectator")
                self.flag_player(player)
                player.mute()
                self.tell_kick(player)
                return True

        return True
    
    @minqlx.delay(1)
    def tell_spec(self, player, time=0):
        self.info("tell_spec")
        player.update()
        if player:
            if player.steam_id in self.loaded_players: # player is connected and should receive tell
                player.tell("^7Sorry, but you need at least ^6{}^7 rating to play here and you are not yet ranked.".format(self.get_cvar("qlx_balance_minimumrating", int)))
                player.tell("^7Type '^6!iam ^2<Your-old-Quakelive-Nick>^7' to get a Rating.")
            else:
                time+=1
                if time<20:
                    self.tell_spec(player, time)
        
    @minqlx.delay(5)
    def tell_kick(self, player, told=False, time=0, kicktime = 40):
        self.info("tell_kick {}".format(time))
        player.update()
        if player:
            if player.steam_id in self.loaded_players and not (told or time == kicktime - 10): # player is connected and should receive tell
                if not told:
                    kicktime = time + 20
                player.tell("^7You do not meet the rating requirements on this server. You will be kicked shortly.")
                told = True
            if time >= kicktime:
                player.ban()
                player.kick()
                return
        time += 5
        if time <= kicktime:
            self.tell_kick(player, told, time, kicktime)
        
    def check_input(self, msg, player, max = 2, min = 1, pos = 1):
        self.info("check_input")
        if len(msg) < min:
            return (minqlx.RET_USAGE, None)
        if len(msg) > max:
            return (minqlx.RET_USAGE, None)
        if len(msg) > pos:
            try:
                i = int(msg[pos])
            except ValueError:
                return (minqlx.RET_USAGE, None)
            target_player = self.player(i)
        else:
            target_player = player
        if not target_player:
            self.msg("Player not found.")
            return (True, None)

        return (False, target_player)
        
    def testnick(self, player, nick, force=False):
        qlranks_key = _quakelive_key.format(player.steam_id)
        if qlranks_key in self.db and not force:
            self.msg("{}'s old quakelive-Nick is already set to {}.".format(player.name, self.db[qlranks_key]))
            return
        self.remove_rating(player.steam_id, self.game.type_short)
        self.fetch_rating([player], self.game.type_short, (self.setnick, (player, nick)), 0, nick)
        
    def setnick(self, player, nick):
        if nick in self.lookup_nicks and self.lookup_nicks[nick][1] == "found":
            qlranks_key = _quakelive_key.format(player.steam_id)
            self.db[qlranks_key] = nick
            self.msg("{}'s old quakelive-Nick is set to {}.".format(player.name, nick))
        else:
            self.msg("{} was not found.".format(nick))
        
    def info(self, info):
        self.console(info)
        return



        
    
class DataGrabber(threading.Thread):
    instances = 0

    def __init__(self, plugin, players):
        threading.Thread.__init__(self)
        self.uid = DataGrabber.instances
        self.plugin = plugin
        self.players = players
        self.status = 0
        DataGrabber.instances += 1
    
    def run(self):
        try:
            try:
                names = []
                for player in self.players:
                    names.append(self.players[player])
                player_list = "+".join(names)
                self.plugin.console("getting ranks for "+player_list)
                data = self.get_data("www.qlranks.com", "/api.aspx?nick={}".format(player_list))
            except:
                self.status = -2
                self.plugin.fetch_rating_datagrabber(None, self)
                return

            if "players" not in data:
                raise Exception("QLRanks returned a valid, but unexpected JSON response.")

            self.plugin.fetch_rating_datagrabber(data, self)
            # Check for pending teams info/balancing needed. Execute if so.
        except:
            self.status = -3
            e = traceback.format_exc().rstrip("\n")
            #minqlx.debug("========== ERROR: QLRanks Fetcher #{} ==========".format(self.uid))
            #for line in e.split("\n"):
            #    minqlx.debug(line)
            self.plugin.fetch_rating_datagrabber(None, self)
    
    def get_data(self, url, path, post_data=None, headers={}):
        c = http.client.HTTPConnection(url, timeout=10)
        if post_data:
            c.request("POST", path, post_data, headers)
        else:
            c.request("GET", path, headers=headers)
        response = c.getresponse()
        self.status = response.status
        
        if response.status == http.client.OK: # 200
            try:
                data = json.loads(response.read().decode())
                return data
            except:
                self.status = -1
                return None
        else:
            return None