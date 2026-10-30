-- SyncVideos.app (droplet)
-- Arraste a pasta "videos" (contendo camA/ e camB/ dentro) — ou as duas
-- pastas camA e camB diretamente — em cima deste app.

property pyScript : "/Users/ilha01/Desktop/PROJETOS/Edit/sync_videos.py"
property pythonBin : "/usr/bin/python3"
property extraPath : "/opt/homebrew/bin"

on open theItems
	set camAPath to ""
	set camBPath to ""

	repeat with anItem in theItems
		set itemPath to POSIX path of anItem
		if itemPath ends with "/" then set itemPath to text 1 thru -2 of itemPath
		set itemName to my lastPathComponent(itemPath)
		set lowerName to my toLower(itemName)

		if lowerName is "cama" then
			set camAPath to itemPath
		else if lowerName is "camb" then
			set camBPath to itemPath
		else
			-- pode ser a pasta-pai contendo camA/ e camB/ dentro
			set candA to itemPath & "/camA"
			set candB to itemPath & "/camB"
			if my posixExists(candA) then set camAPath to candA
			if my posixExists(candB) then set camBPath to candB
		end if
	end repeat

	if camAPath is "" or camBPath is "" then
		display alert "Não encontrei as pastas camA e camB" message "Arraste a pasta que contém camA/ e camB/ dentro, ou arraste as duas pastas (nomeadas exatamente camA e camB) juntas em cima do app." as critical
		return
	end if

	set basePath to my parentPath(camAPath)
	set stamp to do shell script "date +%Y%m%d_%H%M%S"
	set outPath to basePath & "/sincronizado_" & stamp
	set logPath to outPath & "_log.txt"

	do shell script "mkdir -p " & quoted form of outPath

	display notification "Começando a analisar e sincronizar os vídeos…" with title "Sync Videos"

	set shellCmd to "export PATH=" & extraPath & ":$PATH; nohup " & quoted form of pythonBin & " " & quoted form of pyScript & " " & quoted form of camAPath & " " & quoted form of camBPath & " " & quoted form of outPath & " > " & quoted form of logPath & " 2>&1 < /dev/null &"
	do shell script shellCmd

	set donePath to outPath & "/DONE"
	set failedPath to outPath & "/FAILED"
	set progressPath to outPath & "/progress.txt"
	set lastText to ""
	set pollCount to 0
	set maxPolls to 960 -- ~4 horas (15s por poll)

	repeat while pollCount < maxPolls
		delay 15
		set pollCount to pollCount + 1
		if my posixExists(donePath) or my posixExists(failedPath) then exit repeat
		if my posixExists(progressPath) then
			set curText to do shell script "cat " & quoted form of progressPath
			if curText is not lastText then
				display notification curText with title "Sync Videos"
				set lastText to curText
			end if
		end if
	end repeat

	if my posixExists(donePath) then
		display notification "Vídeos sincronizados com sucesso!" with title "Sync Videos" subtitle outPath sound name "Glass"
		do shell script "open " & quoted form of outPath
	else if my posixExists(failedPath) then
		display notification "Deu erro ao sincronizar. Abrindo o log…" with title "Sync Videos" sound name "Basso"
		do shell script "open -a TextEdit " & quoted form of logPath
	else
		display notification "Ainda processando depois de bastante tempo — confira a pasta de saída manualmente." with title "Sync Videos"
	end if
end open

on lastPathComponent(p)
	set AppleScript's text item delimiters to "/"
	set comps to text items of p
	set r to item -1 of comps
	set AppleScript's text item delimiters to ""
	return r
end lastPathComponent

on parentPath(p)
	set AppleScript's text item delimiters to "/"
	set comps to text items of p
	set comps to items 1 thru -2 of comps
	set r to comps as string
	set AppleScript's text item delimiters to ""
	return r
end parentPath

on toLower(s)
	return do shell script "echo " & quoted form of s & " | tr '[:upper:]' '[:lower:]'"
end toLower

on posixExists(p)
	try
		do shell script "test -e " & quoted form of p
		return true
	on error
		return false
	end try
end posixExists
