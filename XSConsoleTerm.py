# Copyright (c) 2007-2009 Citrix Systems Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; version 2 only.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.

import sys, os, time, string
import curses

from XSConsoleAuth import *
from XSConsoleBases import *
from XSConsoleCurses import *
from XSConsoleData import *
from XSConsoleHotData import *
from XSConsoleImporter import *
from XSConsoleMenus import *
from XSConsoleLang import *
from XSConsoleLayout import *
from XSConsoleLog import *
from XSConsoleRemoteTest import *
from XSConsoleRootDialogue import *
from XSConsoleState import *


if sys.version_info >= (3, 0):
    getTimeStamp = time.monotonic
else:
    getTimeStamp = time.time


class App:
    __instance = None

    @classmethod
    def Inst(cls):
        if cls.__instance is None:
            cls.__instance = App()
        return cls.__instance

    def __init__(self):
        self.cursesScreen = None

    def AssertScreenSize(self):
        if hasattr(self, 'layout'):
            self.layout.AssertScreenSize()

    def Build(self, inDirs = None):
        # Search for the app plugins and include them
        Importer.Reset()
        for dir in inDirs:
            Importer.ImportRelativeDir(dir)

    def Enter(self):
        startTime = getTimeStamp()
        Data.Inst().Update()
        elapsedTime = getTimeStamp() - startTime
        XSLog('Loaded initial xapi and system data in %.3f seconds' % elapsedTime)

        doQuit = False

        if '--dump' in sys.argv:
            # Testing - dump data and exit
            Data.Inst().Dump()
            Importer.Dump()
            for key, value in HotData.Inst().guest_vm().items():
                localhost = HotAccessor().local_host()
                vm = HotData.Inst().vm[key]
                vm.metrics()
                try: vm.guest_metrics()
                except: pass # Not all VMs  have guest metrics
                HotAccessor().pool()
            HotData.Inst().Dump()
            doQuit = True

        RemoteTest.Inst().SetApp(self)

        # Reinstate keymap
        if State.Inst().Keymap() is not None:
            Data.Inst().KeymapSet(State.Inst().Keymap())

        while not doQuit:
            try:
                try:
                    if os.path.isfile("/bin/setfont"):
                        os.system("/bin/setfont") # Restore the default font
                    if '-f' in sys.argv:
                        # -f means that this is the automatically started xsonsole on tty1, so set it up to suit xsconsole
                        os.system('/bin/stty quit ^-') # Disable Print Screen key as quit
                        os.system('/bin/stty stop ^-') # Disable Ctrl-S as suspend

                    os.environ["ESCDELAY"] = "50" # Speed up processing of the escape key

                    self.cursesScreen = CursesScreen()
                    self.renderer = Renderer()
                    self.layout = Layout.NewInst()
                    self.layout.ParentSet(self.cursesScreen)
                    self.layout.WriteParentOffset(self.cursesScreen)
                    self.layout.Create()
                    self.layout.ParentSet(self.layout.Window(self.layout.WIN_MAIN))
                    self.layout.CreateRootDialogue(RootDialogue(self.layout, self.layout.Window(self.layout.WIN_MAIN)))
                    self.layout.TransientBannerHandlerSet(App.TransientBannerHandler)

                    if State.Inst().WeStoppedXAPI():
                        # Restart XAPI if we crashed after stopping it
                        Data.Inst().StartXAPI()
                        Data.Inst().Update()

                    Importer.CallReadyHandlers()

                    self.layout.Clear()
                    if not '--dryrun' in sys.argv:
                        self.MainLoop()

                finally:
                    if self.cursesScreen is not None:
                        self.cursesScreen.Exit()

                if self.layout.ExitCommand() is None:
                    doQuit = True
                else:
                    os.system('/usr/bin/reset') # Reset terminal
                    if self.layout.ExitBanner() is not None:
                        reflowed = Language.ReflowText(self.layout.ExitBanner(),  80)
                        for line in reflowed:
                            print(line)
                        sys.stdout.flush()
                    commandList = self.layout.ExitCommand().split()

                    if len(commandList) == 0:
                        doQuit = True
                    else:
                        if self.layout.ExitCommandIsExec():
                            os.execv(commandList[0], commandList)
                            # Does not return
                        else:
                            os.system(self.layout.ExitCommand())
                            Data.Inst().Update() # Pick up changes caused by the subshell command

            except KeyboardInterrupt as e: # Catch Ctrl-C
                XSLog('Resetting due to Ctrl-C')
                Data.Reset()
                sys.stderr.write("\033[H\033[J"+Lang("Resetting...")) # Clear screen and print banner
                try:
                    time.sleep(0.5) # Prevent flicker
                except Exception:
                    pass # Catch repeated Ctrl-C

            except Exception as e:
                sys.stderr.write(Lang(e)+"\n")
                doQuit = True
                raise

    def NeedsRefresh(self):
        self.needsRefresh = True

    def HandleKeypress(self, inKeypress):
        handled = True
        Auth.Inst().KeepAlive()
        self.lastWakeSeconds = getTimeStamp()
        if self.layout.TopDialogue().HandleKey(inKeypress):
            State.Inst().SaveIfRequired()
            self.needsRefresh = True
        elif inKeypress == 'KEY_ESCAPE':
            # Set root menu choice to the first, to give a fixed start state after lots of escapes
            self.layout.TopDialogue().Reset()
            self.needsRefresh = True
        elif inKeypress == 'KEY_F(5)':
            Data.Inst().Update()
            self.layout.UpdateRootFields()
            self.needsRefresh = True
        elif inKeypress == '\014': # Ctrl-L
            Layout.Inst().Clear() # Full redraw
            self.needsRefresh = True
        else:
            handled = False

        return handled

    def MainLoop(self):
        doQuit= False
        startSeconds = getTimeStamp()
        lastDataUpdateSeconds = startSeconds
        lastScreenUpdateSeconds = startSeconds
        lastGarbageCollectSeconds = startSeconds
        self.lastWakeSeconds = startSeconds
        resized = False
        data = Data.Inst()
        errorCount = 0

        self.layout.DoUpdate()
        while not doQuit:
            self.needsRefresh = False
            gotTestCommand = RemoteTest.Inst().Poll()
            secondsNow = getTimeStamp()
            try:
                if gotTestCommand:
                    gotKey = None # Prevent delay whilst waiting for a keypress
                elif secondsNow - self.lastWakeSeconds > State.Inst().SleepSeconds():
                    gotKey = None
                    Layout.Inst().PushDialogue(BannerDialogue(Lang("Press any key to access this console")))
                    Layout.Inst().Refresh()
                    Layout.Inst().DoUpdate()
                    XSLog('Entering sleep due to inactivity - xsconsole is now blocked waiting for a keypress')
                    self.layout.Window(Layout.WIN_MAIN).GetKeyBlocking()
                    XSLog('Exiting sleep')
                    self.lastWakeSeconds = getTimeStamp()
                    self.needsRefresh = True
                    Layout.Inst().PopDialogue()
                else:
                    gotKey = self.layout.Window(Layout.WIN_MAIN).GetKey()

            except Exception as e:
                gotKey = None # Catch timeout

            if gotKey == "\011": gotKey = "KEY_TAB"
            if gotKey == "\012": gotKey = "KEY_ENTER"
            if gotKey == "\033": gotKey = "KEY_ESCAPE"
            if gotKey == "\177": gotKey = "KEY_BACKSPACE"
            if gotKey == '\xc2': gotKey = "KEY_F(5)" # Handle function key mistranslation on vncterm
            if gotKey == '\xc5': gotKey = "KEY_F(8)" # Handle function key mistranslation on vncterm

            if gotKey == 'KEY_RESIZE':
                XSLog('Activity on another console')
                resized = True
            elif resized and gotKey is not None:
                if os.path.isfile("/bin/setfont"): os.system("/bin/setfont") # Restore the default font
                resized = False

            # Screen out non-ASCII and unusual characters
            for char in FirstValue(gotKey, ''):
                if char >="\177": # Characters 128 and greater
                    gotKey = None
                    break

            secondsNow = getTimeStamp()
            secondsRunning = secondsNow - startSeconds

            if data.host.address('') == '' or len(data.derived.managementpifs([])) == 0:
                # If the host doesn't yet have an IP or doesn't have any
                # management PIFs yet, reload data occasionally to pick up
                # DHCP updates
                if secondsNow - lastDataUpdateSeconds >= 4:
                    lastDataUpdateSeconds = secondsNow
                    data.Update()
                    self.layout.UpdateRootFields()
                    self.needsRefresh = True

            if secondsNow - lastScreenUpdateSeconds >= 4:
                lastScreenUpdateSeconds = secondsNow
                self.layout.UpdateRootFields()
                self.needsRefresh = True

            if gotKey is not None:
                try:
                    self.HandleKeypress(gotKey)

                except Exception as e:
                    if Auth.Inst().IsTestMode():
                        raise
                    message = Lang(e) # Also logs the error
                    if errorCount <= 10:
                        if errorCount == 10:
                            message += Lang('\n\n(No more errors will be reported)')
                        errorCount += 1
                        Layout.Inst().PushDialogue(InfoDialogue(Lang("Error"), message))

            if self.layout.ExitCommand() is not None:
                doQuit = True

            brand = Language.Inst().Branding(data.derived.brand())
            version = data.derived.shortversion()

            bannerStr = brand + ' ' + version

            if Auth.Inst().IsAuthenticated():
                hostStr = Auth.Inst().LoggedInUsername()+'@'+data.host.hostname('')
            else:
                hostStr = data.host.hostname('')

            # Testing
            # if gotKey is not None:
            #     bannerStr = gotKey

            timeStr = time.strftime(" %H:%M:%S %Z [UTC%z] ", time.localtime())
            statusLine = ("%-25s%28.28s%27.27s" % (bannerStr[:25], timeStr[:28], hostStr[:27]))
            self.renderer.RenderStatus(self.layout.Window(Layout.WIN_TOPLINE), statusLine)

            if self.needsRefresh:
                self.layout.Refresh()
            elif self.layout.LiveUpdateFields():
                self.layout.Refresh()

            self.layout.DoUpdate()

            if secondsNow - lastGarbageCollectSeconds >= 60:
                lastGarbageCollectSeconds = secondsNow
                Task.Inst().GarbageCollect()

    @classmethod
    def TransientBannerHandler(self, inMessage):
        layout = Layout.Inst()
        layout.PushDialogue(BannerDialogue(inMessage))
        layout.Refresh()
        layout.DoUpdate()
        layout.PopDialogue()

class Renderer:
    def RenderStatus(self, inWindow, inText):
        (cursY, cursX) = curses.getsyx() # Store cursor position
        inWindow.Win().erase()
        inWindow.AddText(inText, 0, 0)
        inWindow.Refresh()
        if cursX != -1 and cursY != -1:
            curses.setsyx(cursY, cursX) # Restore cursor position

