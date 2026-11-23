-- Immutable read-only resource. Caller values arrive only as descriptors.
use framework "Foundation"
use scripting additions

on boundedText(valueText, maxCount)
  if (count characters of valueText) > maxCount then return text 1 thru maxCount of valueText
  return valueText
end boundedText

on queryWords(queryText)
  set parts to (current application's NSString's stringWithString:queryText)'s componentsSeparatedByCharactersInSet:(current application's NSCharacterSet's whitespaceAndNewlineCharacterSet())
  set tokens to {}
  repeat with part in (parts as list)
    if (part as text) is not "" then set end of tokens to part as text
  end repeat
  return tokens
end queryWords

on matchesWords(noteTitle, noteText, tokens)
  ignoring case
    repeat with token in tokens
      if noteTitle does not contain (contents of token) and noteText does not contain (contents of token) then return false
    end repeat
  end ignoring
  return true
end matchesWords

on noteSnippet(noteText, tokens)
  set firstMatch to 0
  ignoring case
    repeat with token in tokens
      set position to offset of (contents of token) in noteText
      if position > 0 and (firstMatch = 0 or position < firstMatch) then set firstMatch to position
    end repeat
  end ignoring
  set startAt to 1
  if firstMatch > 80 then set startAt to firstMatch - 80
  set endAt to startAt + 239
  if endAt > (count characters of noteText) then set endAt to count characters of noteText
  if endAt < startAt then return ""
  return text startAt thru endAt of noteText
end noteSnippet

on isoDate(d)
  -- Resolve the date's own local UTC offset, including historical DST.
  set components to current application's NSDateComponents's alloc()'s init()
  components's setYear:(year of d as integer)
  components's setMonth:(month of d as integer)
  components's setDay:(day of d as integer)
  components's setHour:(hours of d)
  components's setMinute:(minutes of d)
  components's setSecond:(seconds of d)
  set stamp to (current application's NSCalendar's currentCalendar())'s dateFromComponents:components
  set formatter to current application's NSISO8601DateFormatter's alloc()'s init()
  return (formatter's stringFromDate:stamp) as text
end isoDate

on checkProtection(isProtected)
  if isProtected then error "Denied" number -27001
end checkProtection

on checkFolder(folderName)
  ignoring case
    if folderName is "Recently Deleted" then error "Denied" number -27001
  end ignoring
end checkFolder

on checkReadable(n)
  with timeout of 60 seconds
    tell application "/System/Applications/Notes.app"
      -- Password-protected stays excluded even if the owner has unlocked it.
      my checkProtection(password protected of n)
      set parentItem to container of n
      repeat while class of parentItem is folder
        my checkFolder(name of parentItem)
        set parentItem to container of parentItem
      end repeat
    end tell
  end timeout
end checkReadable

on noteRecord(n)
  with timeout of 60 seconds
    my checkReadable(n)
    tell application "/System/Applications/Notes.app"
      set parentFolder to container of n
      set parentItem to parentFolder
      repeat while class of parentItem is folder
        set parentItem to container of parentItem
      end repeat
      return {|id|:id of n, |title|:my boundedText(name of n, 200), |folder|:my boundedText(name of parentFolder, 200), |account|:my boundedText(name of parentItem, 200), |modified|:my isoDate(modification date of n)}
    end tell
  end timeout
end noteRecord

on searchFolder(f, tokens, maxCount, resultRows)
  with timeout of 60 seconds
    tell application "/System/Applications/Notes.app"
      ignoring case
        if name of f is "Recently Deleted" then return {resultRows, false}
      end ignoring
      set folderNotes to notes of f
    end tell
    repeat with n in folderNotes
      set readable to true
      try
        my checkReadable(contents of n)
      on error errorText number errorNumber
        if errorNumber is not -27001 then error "Read failed" number errorNumber
        set readable to false
      end try
      if readable then
        tell application "/System/Applications/Notes.app"
          set noteTitle to name of n
          set noteText to plaintext of n
        end tell
        if my matchesWords(noteTitle, noteText, tokens) then
          if (count resultRows) = maxCount then return {resultRows, true}
          set row to my noteRecord(contents of n)
          set row to row & {|snippet|:my noteSnippet(noteText, tokens)}
          set end of resultRows to row
        end if
      end if
    end repeat
    tell application "/System/Applications/Notes.app" to set childFolders to folders of f
    repeat with childFolder in childFolders
      set found to my searchFolder(contents of childFolder, tokens, maxCount, resultRows)
      set resultRows to item 1 of found
      if item 2 of found then return found
    end repeat
    return {resultRows, false}
  end timeout
end searchFolder

on searchNotes(queryText, maxCount)
  with timeout of 60 seconds
    set tokens to my queryWords(queryText)
    if tokens = {} then error "Invalid query" number -27003
    set resultRows to {}
    tell application "/System/Applications/Notes.app" to set noteAccounts to accounts
    repeat with a in noteAccounts
      tell application "/System/Applications/Notes.app" to set accountFolders to folders of a
      repeat with f in accountFolders
        set found to my searchFolder(contents of f, tokens, maxCount, resultRows)
        set resultRows to item 1 of found
        if item 2 of found then return {|results|:resultRows, |truncated|:true}
      end repeat
    end repeat
    return {|results|:resultRows, |truncated|:false}
  end timeout
end searchNotes

on getNote(noteId)
  with timeout of 60 seconds
    tell application "/System/Applications/Notes.app"
      if not (exists note id noteId) then error "Missing" number -27002
      set n to note id noteId
    end tell
    my checkReadable(n)
    set row to my noteRecord(n)
    tell application "/System/Applications/Notes.app"
      set noteText to plaintext of n
      set createdAt to my isoDate(creation date of n)
    end tell
    return row & {|created|:createdAt, |body|:my boundedText(noteText, 20000), |truncated|:(count characters of noteText) > 20000}
  end timeout
end getNote
