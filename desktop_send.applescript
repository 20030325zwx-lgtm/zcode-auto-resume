on run argv
    set targetTitle to item 1 of argv
    set promptText to item 2 of argv

    tell application "System Events"
        if not (exists process "ZCode") then error "ZCode desktop app is not running"
        set previousApp to name of first application process whose frontmost is true
    end tell
    tell application "ZCode" to activate

    set clipboardChanged to false
    try
        tell application "System Events"
            tell process "ZCode"
                set frontmost to true
                set mainWindow to window "ZCode"
                set windowPosition to position of mainWindow
                set windowSize to size of mainWindow
                set windowLeft to item 1 of windowPosition
                set windowWidth to item 1 of windowSize
                set sidebarLimit to windowLeft + (windowWidth / 3)
                set sidebarMatch to my findSidebarTitle(mainWindow, targetTitle, sidebarLimit)
                if sidebarMatch is missing value then error "Target session is not visible in the ZCode sidebar"
                click sidebarMatch
                delay 0.4
                set visibleTitle to ""
                repeat 6 times
                    set visibleTitle to my activeSessionTitle(sidebarMatch)
                    if visibleTitle is targetTitle then exit repeat
                    delay 0.2
                end repeat
                if visibleTitle is not targetTitle then error "Selected session title did not match"

                set composer to my findComposer(mainWindow)
                if composer is missing value then error "ZCode composer was not found"
                set composerPanel to composer
                repeat 3 times
                    set composerPanel to value of attribute "AXParent" of composerPanel
                end repeat
                set sendButton to missing value
                repeat with nearItem in UI elements of composerPanel
                    try
                        if role of nearItem is "AXButton" and (description of nearItem as text) is "发送" then set sendButton to nearItem
                    end try
                end repeat
                if sendButton is missing value then error "ZCode send button was not found"
                if my normalizedComposerValue(value of composer) is not "" then error "ZCode composer contains an unsent draft"

                click composer
                set savedClipboard to the clipboard
                set clipboardChanged to true
                set the clipboard to promptText
                keystroke "v" using command down
                delay 0.35
                set pastedText to my normalizedComposerValue(value of composer)
                set the clipboard to savedClipboard
                set clipboardChanged to false
                if pastedText is not promptText then
                    keystroke "a" using command down
                    key code 51
                    error "Pasted prompt did not match the ZCode composer"
                end if
                set composer to my findComposer(mainWindow)
                if composer is missing value then error "ZCode composer disappeared before sending"
                set composerPanel to composer
                repeat 3 times
                    set composerPanel to value of attribute "AXParent" of composerPanel
                end repeat
                set sendButton to missing value
                repeat with nearItem in UI elements of composerPanel
                    try
                        if role of nearItem is "AXButton" and (description of nearItem as text) is "发送" then set sendButton to nearItem
                    end try
                end repeat
                if sendButton is missing value then error "ZCode send button disappeared before sending"
                if not (enabled of sendButton) then error "ZCode send button is disabled"
                click composer
                key code 36
            end tell
            try
                if previousApp is not "ZCode" and (exists process previousApp) then
                    set frontmost of process previousApp to true
                end if
            end try
        end tell
        return "submitted"
    on error errorText
        if clipboardChanged then
            try
                set the clipboard to savedClipboard
            end try
        end if
        tell application "System Events"
            try
                if previousApp is not "ZCode" and (exists process previousApp) then
                    set frontmost of process previousApp to true
                end if
            end try
        end tell
        error errorText
    end try
end run

on findSidebarTitle(rootElement, targetTitle, sidebarLimit)
    tell application "System Events"
        try
            if role of rootElement is "AXStaticText" and (value of rootElement as text) is targetTitle then
                set itemPosition to position of rootElement
                if item 1 of itemPosition < sidebarLimit then return rootElement
            end if
            set childItems to UI elements of rootElement
            repeat with childItem in childItems
                set foundItem to my findSidebarTitle(contents of childItem, targetTitle, sidebarLimit)
                if foundItem is not missing value then return foundItem
            end repeat
        end try
    end tell
    return missing value
end findSidebarTitle

on activeSessionTitle(sidebarItem)
    tell application "System Events"
        set layoutItem to sidebarItem
        repeat 30 times
            try
                set layoutItem to value of attribute "AXParent" of layoutItem
                set siblingItems to UI elements of layoutItem
                repeat with siblingIndex from 1 to (count of siblingItems)
                    if role of item siblingIndex of siblingItems is "AXSplitter" then
                        if siblingIndex is (count of siblingItems) then return ""
                        set headerNode to item (siblingIndex + 1) of siblingItems
                        repeat 12 times
                            set headerChildren to UI elements of headerNode
                            repeat with headerChild in headerChildren
                                if role of headerChild is "AXHeading" then return name of headerChild as text
                            end repeat
                            if (count of headerChildren) is 0 then return ""
                            set headerNode to item 1 of headerChildren
                        end repeat
                        return ""
                    end if
                end repeat
            on error
                return ""
            end try
        end repeat
    end tell
    return ""
end activeSessionTitle

on findComposer(rootElement)
    tell application "System Events"
        try
            if role of rootElement is "AXTextArea" then
                set placeholderText to value of attribute "AXPlaceholderValue" of rootElement
                if placeholderText contains "后续修改要求" then return rootElement
            end if
            set childItems to UI elements of rootElement
            repeat with childIndex from (count of childItems) to 1 by -1
                set foundItem to my findComposer(item childIndex of childItems)
                if foundItem is not missing value then return foundItem
            end repeat
        end try
    end tell
    return missing value
end findComposer

on normalizedComposerValue(rawValue)
    if rawValue is missing value then return ""
    set composerText to rawValue as text
    repeat while composerText ends with linefeed or composerText ends with return
        if (length of composerText) is 1 then return ""
        set composerText to text 1 thru ((length of composerText) - 1) of composerText
    end repeat
    return composerText
end normalizedComposerValue
