-- Fixed source only. All caller data arrives as Apple Event descriptors.
-- Never evaluate caller strings as AppleScript or shell commands.
on isoDay(d)
  with timeout of 10 seconds
    if d is missing value then return ""
    set y to year of d as integer
    set m to month of d as integer
    set n to day of d as integer
    return (y as text) & "-" & text -2 thru -1 of ("0" & m) & "-" & text -2 thru -1 of ("0" & n)
  end timeout
end isoDay

on dayValue(s)
  with timeout of 10 seconds
    set d to current date
    set time of d to 0
    set day of d to 1
    set year of d to (text 1 thru 4 of s) as integer
    set month of d to (text 6 thru 7 of s) as integer
    set day of d to (text 9 thru 10 of s) as integer
    return d
  end timeout
end dayValue

on destination(t)
  with timeout of 10 seconds
    set pid to ""
    set pn to ""
    set aid to ""
    set an to ""
    tell application "/Applications/Things3.app"
      set p to project of t
      if p is not missing value then
        set pid to id of p
        set pn to name of p
        set a to area of p
      else
        set a to area of t
      end if
      if a is not missing value then
        set aid to id of a
        set an to name of a
      end if
    end tell
    return {pid, pn, aid, an}
  end timeout
end destination

on checkDestination(pid, aid, allowAll, projects, areas, denyProjects, denyAreas)
  with timeout of 10 seconds
    if (pid is not "" and denyProjects contains pid) or (aid is not "" and denyAreas contains aid) then error "Denied" number -27001
    if allowAll or (pid is "" and aid is "") then return
    if (pid is not "" and projects contains pid) or (aid is not "" and areas contains aid) then return
    error "Denied" number -27001
  end timeout
end checkDestination

on todoRecord(t)
  with timeout of 10 seconds
    set dest to my destination(t)
    tell application "/Applications/Things3.app"
      set taskNotes to notes of t
      if taskNotes is missing value then set taskNotes to ""
      if (length of taskNotes) > 2000 then set taskNotes to text 1 thru 2000 of taskNotes
      set taskState to "open"
      if status of t is completed then set taskState to "completed"
      if status of t is canceled then set taskState to "canceled"
      set taskTags to name of tags of t
      set dueDay to my isoDay(due date of t)
      set activationDay to my isoDay(activation date of t)
      return {|id|:id of t, |name|:name of t, |notes|:taskNotes, |status|:taskState, |due|:dueDay, |deadline|:dueDay, |activation_date|:activationDay, |project_id|:item 1 of dest, |project_name|:item 2 of dest, |area_id|:item 3 of dest, |area_name|:item 4 of dest, |tags|:taskTags}
    end tell
  end timeout
end todoRecord

on collectTodos(ts, maxCount)
  with timeout of 10 seconds
    set resultRows to {}
    set capped to false
    repeat with t in ts
      tell application "/Applications/Things3.app" to set isTodo to {to do, selected to do} contains class of t
      if isTodo then
        if (count resultRows) = maxCount then
          set capped to true
          exit repeat
        end if
        set end of resultRows to my todoRecord(contents of t)
      end if
    end repeat
    return {|todos|:resultRows, |truncated|:capped}
  end timeout
end collectTodos

on listTodos(listName, maxCount)
  with timeout of 10 seconds
    tell application "/Applications/Things3.app" to set ts to to dos of list listName
    return my collectTodos(ts, maxCount)
  end timeout
end listTodos

on searchTodos(queryText, maxCount)
  with timeout of 10 seconds
    tell application "/Applications/Things3.app" to set ts to every to do whose name contains queryText
    return my collectTodos(ts, maxCount)
  end timeout
end searchTodos

on listProjects()
  with timeout of 10 seconds
    set resultRows to {}
    tell application "/Applications/Things3.app" to set ps to projects
    repeat with p in ps
      if (count resultRows) = 200 then exit repeat
      set end of resultRows to my todoRecord(contents of p)
    end repeat
    return {|projects|:resultRows, |truncated|:(count ps) > 200}
  end timeout
end listProjects

on listAreas()
  with timeout of 10 seconds
    set resultRows to {}
    tell application "/Applications/Things3.app"
      set aa to areas
      repeat with a in aa
        if (count resultRows) = 200 then exit repeat
        set end of resultRows to {|id|:id of a, |name|:name of a}
      end repeat
    end tell
    return {|areas|:resultRows, |truncated|:(count aa) > 200}
  end timeout
end listAreas

on applyDates(t, whenValue, deadlineValue, changeDeadline)
  with timeout of 10 seconds
    tell application "/Applications/Things3.app"
      if changeDeadline then
        if deadlineValue is "" then
          delete due date of t  -- Things clears a deadline with delete, not missing value
        else
          set due date of t to my dayValue(deadlineValue)
        end if
      end if
      if whenValue is "someday" then
        move t to list "Someday"
      else if whenValue is "anytime" then
        move t to list "Anytime"
      else if whenValue is not "" then
        if whenValue is "today" or whenValue is "tomorrow" then
          set scheduledDay to current date
          set time of scheduledDay to 0
          if whenValue is "tomorrow" then set scheduledDay to scheduledDay + days
        else
          set scheduledDay to my dayValue(whenValue)
        end if
        schedule t for scheduledDay
      end if
    end tell
  end timeout
end applyDates

on addTodo(taskTitle, taskNotes, whenValue, deadlineValue, projectID, areaID, tagNames, checklistItems, allowAll, projects, areas, denyProjects, denyAreas)
  with timeout of 10 seconds
    -- The installed dictionary has no checklist/Evening property. Refuse before make.
    if (count checklistItems) > 0 or whenValue is in {"tonight", "evening"} then error "Unsupported" number -27004
    set parentArea to areaID
    tell application "/Applications/Things3.app"
      if projectID is not "" then
        if not (exists project id projectID) then error "Missing" number -27002
        set targetProject to project id projectID
        set a to area of targetProject
        if a is not missing value then set parentArea to id of a
      else if areaID is not "" then
        if not (exists area id areaID) then error "Missing" number -27002
      end if
      my checkDestination(projectID, parentArea, allowAll, projects, areas, denyProjects, denyAreas)
      -- Check every tag before creating; Things must not create new tag names.
      repeat with tagName in tagNames
        if not (exists tag named (contents of tagName)) then error "Unknown tag" number -27003
      end repeat
      set newProperties to {name:taskTitle, notes:taskNotes}
      set t to make new to do with properties newProperties at beginning of list "Inbox"
      if projectID is not "" then
        set project of t to targetProject
      else if areaID is not "" then
        set area of t to area id areaID
      end if
      if (count tagNames) > 0 then my applyTags(t, tagNames)
      my applyDates(t, whenValue, deadlineValue, deadlineValue is not "")
    end tell
    return {|todo|:my todoRecord(t)}
  end timeout
end addTodo

on completeTodo(todoID, allowAll, projects, areas, denyProjects, denyAreas)
  with timeout of 10 seconds
    tell application "/Applications/Things3.app"
      if not (exists to do id todoID) then error "Missing" number -27002
      set t to to do id todoID
      -- Projects inherit from to do; this capability must never complete a project.
      -- Things reports ordinary to-dos as "selected to do" in many lists.
      if {to do, selected to do} does not contain class of t then error "Denied" number -27001
      set dest to my destination(t)
      my checkDestination(item 1 of dest, item 3 of dest, allowAll, projects, areas, denyProjects, denyAreas)
      if status of t is not completed then set status of t to completed
    end tell
    return {|todo|:my todoRecord(t)}
  end timeout
end completeTodo

-- All optional values have separate presence flags: empty notes/tags and a
-- cleared deadline must not be confused with omitted fields.
on updateTodo(todoID, hasTitle, taskTitle, hasNotes, taskNotes, hasWhen, whenValue, hasDeadline, deadlineValue, hasProject, projectID, hasArea, areaID, hasTags, tagNames, allowAll, projects, areas, denyProjects, denyAreas)
  with timeout of 10 seconds
    tell application "/Applications/Things3.app"
      if not (exists to do id todoID) then error "Missing" number -27002
      set t to to do id todoID
      if {to do, selected to do} does not contain class of t then error "Denied" number -27001
      -- Closed to-dos refuse every edit, including title and notes.
      if status of t is not open then error "Denied" number -27001
      set dest to my destination(t)
      my checkDestination(item 1 of dest, item 3 of dest, allowAll, projects, areas, denyProjects, denyAreas)
      -- Resolve the new parent's area before any change, even to title/notes.
      if hasProject then
        if not (exists project id projectID) then error "Missing" number -27002
        set targetProject to project id projectID
        set parentArea to ""
        set a to area of targetProject
        if a is not missing value then set parentArea to id of a
        my checkDestination(projectID, parentArea, allowAll, projects, areas, denyProjects, denyAreas)
      else if hasArea then
        if not (exists area id areaID) then error "Missing" number -27002
        my checkDestination("", areaID, allowAll, projects, areas, denyProjects, denyAreas)
      end if
      if hasTags then
        repeat with tagName in tagNames
          if not (exists tag named (contents of tagName)) then error "Unknown tag" number -27003
        end repeat
      end if
      -- All policy and tag checks precede the first mutation.
      if hasProject then
        set project of t to targetProject
      else if hasArea then
        set project of t to missing value
        set area of t to area id areaID
      end if
      if hasTitle then set name of t to taskTitle
      if hasNotes then set notes of t to taskNotes
      if hasTags then my applyTags(t, tagNames)
      my applyDates(t, whenValue, deadlineValue, hasDeadline)
    end tell
    return {|todo|:my todoRecord(t)}
  end timeout
end updateTodo

on applyTags(t, tagNames)
  with timeout of 10 seconds
    tell application "/Applications/Things3.app"
        set oldDelimiters to AppleScript's text item delimiters
        try
          set AppleScript's text item delimiters to ", "
          set joinedTags to tagNames as text
          set AppleScript's text item delimiters to oldDelimiters
        on error
          set AppleScript's text item delimiters to oldDelimiters
          error "Tags failed" number -27003
        end try
        set tag names of t to joinedTags
    end tell
  end timeout
end applyTags
