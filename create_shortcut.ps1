$ws = New-Object -ComObject WScript.Shell
$desktop = [Environment]::GetFolderPath('Desktop')
$shortcut = $ws.CreateShortcut("$desktop\md2pdf.lnk")
$shortcut.TargetPath = "C:\Users\AUDAX-CAIRO\Sites\md2pdf\md2pdf.vbs"
$shortcut.WorkingDirectory = "C:\Users\AUDAX-CAIRO\Sites\md2pdf"
$shortcut.Description = "md2pdf - Markdown to PDF"
$shortcut.IconLocation = "C:\Windows\System32\shell32.dll,70"
$shortcut.Save()
Write-Host "Atalho 'md2pdf' criado na area de trabalho!"
