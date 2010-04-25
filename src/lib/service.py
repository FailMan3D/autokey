#!/usr/bin/env python
# -*- coding: utf-8 -*-

# Copyright (C) 2008 Chris Dekter

# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 675 Mass Ave, Cambridge, MA 02139, USA.

import time, logging, threading, traceback
import common
from iomediator import Key, IoMediator
from configmanager import *
if common.USING_QT:
    from qtui.popupmenu import *
else:
    from gtkui.popupmenu import *
import scripting, model

logger = logging.getLogger("service")

MAX_STACK_LENGTH = 150

def threaded(f):
    
    def wrapper(*args):
        t = threading.Thread(target=f, args=args, name="Phrase-thread")
        t.setDaemon(False)
        t.start()
        
    wrapper.__name__ = f.__name__
    wrapper.__dict__ = f.__dict__
    wrapper.__doc__ = f.__doc__
    return wrapper

def synchronized(lock):
    """ Synchronization decorator. """

    def wrap(f):
        def new_function(*args, **kw):
            lock.acquire()
            try:
                return f(*args, **kw)
            finally:
                lock.release()
        return new_function
    return wrap


class Service:
    """
    Handles general functionality and dispatching of results down to the correct
    execution service (phrase or script).
    """
    
    def __init__(self, app):
        logger.info("Starting service")
        self.configManager = app.configManager
        ConfigManager.SETTINGS[SERVICE_RUNNING] = False
        self.mediator = None
        self.app = app
        self.inputStack = []
        self.lastStackState = ''
        self.lastMenu = None
        
    def start(self):
        self.mediator = IoMediator(self)
        ConfigManager.SETTINGS[SERVICE_RUNNING] = True
        self.phraseRunner = PhraseRunner(self)
        self.scriptRunner = ScriptRunner(self.mediator, self.app)
        logger.info("Service now marked as running")
        
    def unpause(self):
        ConfigManager.SETTINGS[SERVICE_RUNNING] = True
        logger.info("Unpausing - service now marked as running")
        
    def pause(self):
        ConfigManager.SETTINGS[SERVICE_RUNNING] = False
        logger.info("Pausing - service now marked as stopped")
        
    def is_running(self):
        return ConfigManager.SETTINGS[SERVICE_RUNNING]
            
    def shutdown(self, save=True):
        logger.info("Service shutting down")
        if self.mediator is not None: self.mediator.shutdown()
        if save: save_config(self.configManager)
            
    def handle_mouseclick(self, rootX, rootY, relX, relY, button, windowTitle):
        logger.debug("Received mouse click - resetting buffer")        
        self.inputStack = []
        
        # If we had a menu and receive a mouse click, means we already
        # hid the menu. Don't need to do it again
        self.lastMenu = None
        
        # Clear last to prevent undo of previous phrase in unexpected places
        self.phraseRunner.clear_last() 
        
    def handle_hotkey(self, key, modifiers, windowName):
        logger.debug("Key: %s, modifiers: %s", repr(key), modifiers)
        
        # Always check global hotkeys
        for hotkey in self.configManager.globalHotkeys:
            hotkey.check_hotkey(modifiers, key, windowName)
            
        if self.__shouldProcess(windowName):
            self.inputStack = []
            itemMatch = None
            menu = None

            for item in self.configManager.hotKeys:
                if item.check_hotkey(modifiers, key, windowName):
                    itemMatch = item
                    break

            if itemMatch is not None:
                if not itemMatch.prompt:
                    logger.info("Matched hotkey phrase/script with prompt=False")
                else:
                    logger.info("Matched hotkey phrase/script with prompt=True")
                    #menu = PopupMenu(self, [], [itemMatch])
                    menu = ([], [itemMatch])
                    
            else:
                logger.debug("No phrase/script matched hotkey")
                for folder in self.configManager.hotKeyFolders:
                    if folder.check_hotkey(modifiers, key, windowName):
                        #menu = PopupMenu(self, [folder], [])
                        menu = ([folder], [])

            
            if menu is not None:
                logger.debug("Folder matched hotkey - showing menu")
                if self.lastMenu is not None:
                    #self.lastMenu.remove_from_desktop()
                    self.app.hide_menu()
                self.lastStackState = ''
                self.lastMenu = menu
                #self.lastMenu.show_on_desktop()
                self.app.show_popup_menu(*menu)
            
            if itemMatch is not None:
                self.__processItem(itemMatch)
        
    def handle_keypress(self, key, windowName):
        logger.debug("Key: %s", key)
        
        if self.__shouldProcess(windowName):
            if self.__updateStack(key):
                currentInput = ''.join(self.inputStack)
                item, menu = self.__checkTextMatches([], self.configManager.abbreviations,
                                                    currentInput, windowName, True)
                if not item or menu:
                    item, menu = self.__checkTextMatches(self.configManager.allFolders,
                                                         self.configManager.allItems,
                                                         currentInput, windowName)
                                                         
                if item:
                    self.__processItem(item, currentInput)
                elif menu:
                    if self.lastMenu is not None:
                        #self.lastMenu.remove_from_desktop()
                        self.app.hide_menu()
                    self.lastMenu = menu
                    #self.lastMenu.show_on_desktop()
                    self.app.show_popup_menu(*menu)
                
                logger.debug("Input stack at end of handle_keypress: %s", self.inputStack)
                
                
    @threaded
    def item_selected(self, item):
        time.sleep(0.25) # wait for window to be active
        self.lastMenu = None # if an item has been selected, the menu has been hidden
        self.__processItem(item, self.lastStackState)
        
    def calculate_extra_keys(self, buffer):
        """
        Determine extra keys pressed since the given buffer was built
        """
        extraBs = len(self.inputStack) - len(buffer)
        if extraBs > 0:
            extraKeys = ''.join(self.inputStack[len(buffer)])
        else:
            extraBs = 0
            extraKeys = ''
        return (extraBs, extraKeys)

    def __updateStack(self, key):
        """
        Update the input stack in non-hotkey mode, and determine if anything
        further is needed.
        
        @return: True if further action is needed
        """
        if self.lastMenu is not None:
            if not ConfigManager.SETTINGS[MENU_TAKES_FOCUS]:
                self.app.hide_menu()
                
            self.lastMenu = None
            
        if key == Key.ENTER:
            # Special case - map Enter to \n
            key = '\n'
            
        if key == Key.BACKSPACE:
            if ConfigManager.SETTINGS[UNDO_USING_BACKSPACE] and self.phraseRunner.can_undo():
                self.phraseRunner.undo_expansion()
            else:
                # handle backspace by dropping the last saved character
                self.inputStack = self.inputStack[:-1]
            
            return False
            
        elif len(key) > 1:
            # non-simple key
            self.inputStack = []
            self.phraseRunner.clear_last()
            return False
        else:
            # Key is a character
            self.phraseRunner.clear_last()
            self.inputStack.append(key)
            if len(self.inputStack) > MAX_STACK_LENGTH:
                self.inputStack.pop(0)
            return True
            
    def __checkTextMatches(self, folders, items, buffer, windowName, immediate=False):
        """
        Check for an abbreviation/predictive match among the given folder and items 
        (scripts, phrases).
        
        @return: a tuple possibly containing an item to execute, or a menu to show
        """
        itemMatches = []
        folderMatches = []
        
        for item in items:
            if item.check_input(buffer, windowName):
                if not item.prompt and immediate:
                    return (item, None)
                else:
                    itemMatches.append(item)
                    
        for folder in folders:
            if folder.check_input(buffer, windowName):
                folderMatches.append(folder)
                break # There should never be more than one folder match anyway
        
        if self.__menuRequired(folderMatches, itemMatches, buffer):
            self.lastStackState = buffer
            #return (None, PopupMenu(self, folderMatches, itemMatches))
            return (None, (folderMatches, itemMatches))
        elif len(itemMatches) == 1:
            self.lastStackState = buffer
            return (itemMatches[0], None)
        else:
            return (None, None)
            
                
    def __shouldProcess(self, windowName):
        """
        Return a boolean indicating whether we should take any action on the keypress
        """
        return windowName != "Set Abbreviation" and self.is_running()
        
    def __processItem(self, item, buffer=''):
        if isinstance(item, model.Phrase):
            self.phraseRunner.execute(item, buffer)
        else:
            self.scriptRunner.execute(item, buffer)
        
        self.inputStack = []
        self.lastStackState = ''
        
    def __haveMatch(self, data):
        folderMatch, itemMatches = data
        if folder is not None:
            return True
        if len(items) > 0:
            return True
            
        return False
        
    def __menuRequired(self, folders, items, buffer):
        """
        @return: a boolean indicating whether a menu is needed to allow the user to choose
        """
        if len(folders) > 0:
            # Folders always need a menu
            return True
        if len(items) == 1:
            return items[0].should_prompt(buffer)
        elif len(items) > 1:
            # More than one 'item' (phrase/script) needs a menu
            return True
            
        return False
        

class PhraseRunner:
    
    def __init__(self, service):
        self.service = service
        #self.pluginManager = PluginManager()
        self.lastExpansion = None
        self.lastPhrase = None  
        self.lastBuffer = None

    @synchronized(iomediator.SEND_LOCK)
    def execute(self, phrase, buffer):
        mediator = self.service.mediator
        mediator.interface.begin_send()
        
        expansion = phrase.build_phrase(buffer)
        
        mediator.send_backspace(expansion.backspaces)
        if phrase.sendMode == model.SendMode.KEYBOARD:
            mediator.send_string(expansion.string)
        else:
            mediator.paste_string(expansion.string, phrase.sendMode)
        mediator.interface.finish_send()

        self.lastExpansion = expansion
        self.lastPhrase = phrase
        self.lastBuffer = buffer
        
    def can_undo(self):
        if self.lastExpansion is not None:
            return model.TriggerMode.ABBREVIATION in self.lastPhrase.modes
            
    def clear_last(self):
        self.lastExpansion = None
        self.lastPhrase = None 

    @synchronized(iomediator.SEND_LOCK)
    def undo_expansion(self):
        logger.info("Undoing last abbreviation expansion")
        replay = self.lastPhrase.get_trigger_chars(self.lastBuffer)
        logger.debug("Replay string: %s", replay)
        logger.debug("Erase string: %r", self.lastExpansion.string)
        mediator = self.service.mediator
        
        #mediator.send_right(self.lastExpansion.lefts)
        mediator.interface.begin_send()
        mediator.remove_string(self.lastExpansion.string)
        mediator.send_string(replay)
        mediator.interface.finish_send()
        self.clear_last()
    
    
class ScriptRunner:
    
    def __init__(self, mediator, app):
        self.mediator = mediator
        self.error = ''
        self.scope = globals()
        self.scope["keyboard"]= scripting.Keyboard(mediator)
        self.scope["mouse"]= scripting.Mouse(mediator)
        self.scope["system"] = scripting.System()
        self.scope["window"] = scripting.Window(mediator)
        self.scope["engine"] = scripting.Engine(app.configManager, self)

        if common.USING_QT:
            self.scope["dialog"] = scripting.QtDialog()
            self.scope["clipboard"] = scripting.QtClipboard(app)
        else:
            self.scope["dialog"] = scripting.GtkDialog()
            self.scope["clipboard"] = scripting.GtkClipboard(app)
        
    def execute(self, script, buffer):
        logger.debug("Script runner executing: %r", script)
        
        # TODO temporary code - remove ASAP
        if not hasattr(script, "store"):
            script.store = scripting.Store()
            
        self.scope["store"] = script.store
        
        backspaces, stringAfter = script.process_buffer(buffer)
        self.mediator.send_backspace(backspaces)

        try:
            exec script.code in self.scope
        except Exception, e:
            logger.exception("Script error")
            self.error = traceback.format_exc()
            
        self.mediator.send_string(stringAfter)
        
