// Test: Use Electron as Node.js to access ai-agent's native module.
// trae-solo-cn runs Electron with ELECTRON_RUN_AS_NODE=1, executing cli.js.
// We try to require the ai-agent native addon directly.
//
// From meta.json, ai-agent's entry is start.bat which runs ai-agent.exe.
// But ai_agent.dll exports v8/rusty_v8 functions, suggesting it's a V8 addon.
// Let's check if we can find and load it.

const path = require('path');
const fs = require('fs');

const aiAgentDir = 'c:\\Users\\Administrator\\AppData\\Local\\Programs\\TRAE SOLO CN\\resources\\app\\modules\\ai-agent';

// List all files in ai-agent directory
console.log('=== Files in ai-agent directory ===');
function listDir(dir, depth = 0) {
    const items = fs.readdirSync(dir, { withFileTypes: true });
    for (const item of items) {
        const fullPath = path.join(dir, item.name);
        if (item.isDirectory() && depth < 1) {
            console.log(`${'  '.repeat(depth)}[DIR] ${item.name}/`);
            listDir(fullPath, depth + 1);
        } else if (item.isFile()) {
            const ext = path.extname(item.name).toLowerCase();
            if (['.dll', '.node', '.exe', '.json', '.bat', '.sh'].includes(ext)) {
                console.log(`${'  '.repeat(depth)}${item.name}`);
            }
        }
    }
}
listDir(aiAgentDir);

// Try to find .node files
console.log('\n=== Searching for .node files ===');
function findNodeFiles(dir, depth = 0) {
    if (depth > 3) return;
    try {
        const items = fs.readdirSync(dir, { withFileTypes: true });
        for (const item of items) {
            const fullPath = path.join(dir, item.name);
            if (item.isDirectory()) {
                findNodeFiles(fullPath, depth + 1);
            } else if (item.name.endsWith('.node')) {
                console.log(`Found: ${fullPath}`);
            }
        }
    } catch (e) {}
}
findNodeFiles('c:\\Users\\Administrator\\AppData\\Local\\Programs\\TRAE SOLO CN\\resources');

// Try to load ai_agent.dll as a Node.js native addon
console.log('\n=== Trying to load ai_agent.dll ===');
const dllPath = path.join(aiAgentDir, 'ai_agent.dll');
try {
    // Method 1: require with .dll extension
    const mod = process.dlopen(module, dllPath);
    console.log('Loaded via process.dlopen!');
    console.log('Module keys:', Object.keys(mod));
} catch (e) {
    console.log(`process.dlopen failed: ${e.message}`);
}

// Try to find the Electron app's main process module
console.log('\n=== Checking Electron app structure ===');
const appOutDir = 'c:\\Users\\Administrator\\AppData\\Local\\Programs\\TRAE SOLO CN\\resources\\app\\out';
try {
    const mainJs = fs.readFileSync(path.join(appOutDir, 'main.js'), 'utf8');
    // Search for ai-agent related code
    const aiAgentMatches = mainJs.match(/ai.?agent|lite.*send.*message|aha_ipc/gi);
    if (aiAgentMatches) {
        console.log('Found ai-agent references in main.js:', [...new Set(aiAgentMatches)].slice(0, 10));
    }
    // Check file size
    console.log(`main.js size: ${mainJs.length} chars`);
} catch (e) {
    console.log(`Error reading main.js: ${e.message}`);
}

// Check if we can access Electron's ipcMain
console.log('\n=== Checking Electron IPC ===');
try {
    const electron = require('electron');
    console.log('Electron available:', Object.keys(electron).slice(0, 10));
} catch (e) {
    console.log(`Electron not available: ${e.message}`);
}

console.log('\nDone.');
