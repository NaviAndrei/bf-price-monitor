param (
    [Parameter(Mandatory=$true)]
    [int]$IssueNumber,

    [Parameter(Mandatory=$true)]
    [ValidateSet("Backlog", "Todo", "In Progress", "In Review", "Done", "Blocked")]
    [string]$Status
)

$owner = "NaviAndrei"
$repo = "bf-price-monitor"

# 1. Fetch Project ID and Number
$projects = gh project list --owner $owner --format json | ConvertFrom-Json
$project = $projects.projects | Where-Object { $_.title -like "bf-price-monitor Roadmap*" } | Select-Object -First 1

if (-not $project) {
    Write-Error "Project 'bf-price-monitor Roadmap' not found for owner '$owner'."
    exit 1
}

$projectNum = $project.number
$projectId = $project.id

# 2. Get the Status field and the target Option ID
$fields = gh project field-list $projectNum --owner $owner --format json | ConvertFrom-Json
$statusField = $fields.fields | Where-Object { $_.name -eq "Status" }
$targetOption = $statusField.options | Where-Object { $_.name -ieq $Status }

if (-not $targetOption) {
    Write-Error "Status option '$Status' not found in Project Status field."
    exit 1
}

# 3. Find the Project Item ID matching the given Issue number
$items = gh project item-list $projectNum --owner $owner --limit 100 --format json | ConvertFrom-Json
$item = $items.items | Where-Object { $_.content.number -eq $IssueNumber }

if (-not $item) {
    Write-Error "Issue #$IssueNumber not found in project '$($project.title)'."
    exit 1
}

# 4. Update the item's Status in Project v2
gh project item-edit `
    --id $item.id `
    --project-id $projectId `
    --field-id $statusField.id `
    --single-select-option-id $targetOption.id | Out-Null

Write-Host "Updated #$IssueNumber status to '$Status' in Project." -ForegroundColor Green