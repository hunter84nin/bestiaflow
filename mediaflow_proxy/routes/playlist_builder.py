import json
import urllib.parse
from typing import Iterator, Dict, Any, Optional
from fastapi import APIRouter, Request, Response, HTTPException, Query
from fastapi.responses import StreamingResponse
import httpx
from mediaflow_proxy.configs import settings
import asyncio

playlist_builder_router = APIRouter()


def rewrite_m3u_links_streaming(m3u_lines_iterator: Iterator[str], base_url: str, api_password: Optional[str]) -> Iterator[str]:
    """
    Riscrive i link da un iteratore di linee M3U secondo le regole specificate,
    includendo gli headers da #EXTVLCOPT e #EXTHTTP. Yields rewritten lines.
    """
    current_ext_headers: Dict[str, str] = {}  # Dizionario per conservare gli headers dalle direttive
    
    for line_with_newline in m3u_lines_iterator:
        line_content = line_with_newline.rstrip('\n')
        logical_line = line_content.strip()
        
        is_header_tag = False
        if logical_line.startswith('#EXTVLCOPT:'):
            is_header_tag = True
            try:
                option_str = logical_line.split(':', 1)[1]
                if '=' in option_str:
                    key_vlc, value_vlc = option_str.split('=', 1)
                    key_vlc = key_vlc.strip()
                    value_vlc = value_vlc.strip()
 
                    # Gestione speciale per http-header che contiene "Key: Value"
                    if key_vlc == 'http-header' and ':' in value_vlc:
                        header_key, header_value = value_vlc.split(':', 1)
                        header_key = header_key.strip()
                        header_value = header_value.strip()
                        current_ext_headers[header_key] = header_value
                    elif key_vlc.startswith('http-'):
                        # Gestisce http-user-agent, http-referer etc.
                        header_key = '-'.join(word.capitalize() for word in key_vlc[len('http-'):].split('-'))
                        current_ext_headers[header_key] = value_vlc
            except Exception as e:
                print(f"⚠️ Errore nel parsing di #EXTVLCOPT '{logical_line}': {e}")
        
        elif logical_line.startswith('#EXTHTTP:'):
            is_header_tag = True
            try:
                json_str = logical_line.split(':', 1)[1]
                # Sostituisce tutti gli header correnti con quelli del JSON
                current_ext_headers = json.loads(json_str)
            except Exception as e:
                print(f"⚠️ Errore nel parsing di #EXTHTTP '{logical_line}': {e}")
                current_ext_headers = {}  # Resetta in caso di errore

        if is_header_tag:
            yield line_with_newline
            continue
        
        if logical_line and not logical_line.startswith('#') and \
           ('http://' in logical_line or 'https://' in logical_line):
            
            # Decide la logica di riscrittura in base alla presenza della password
            if api_password is not None:
                # --- LOGICA CON PASSWORD (MFP) ---
                processed_url_content = logical_line
                
                # Non modificare link pluto.tv
                if 'pluto.tv' in logical_line:
                    processed_url_content = logical_line
                elif 'vavoo.to' in logical_line:
                    processed_url_content = f"{base_url}/proxy/hls/manifest.m3u8?api_password={api_password}&d={logical_line}"
                elif 'vixsrc.to' in logical_line:
                    processed_url_content = f"{base_url}/extractor/video?host=VixCloud&redirect_stream=true&api_password={api_password}&d={logical_line}"
                elif '.m3u8' in logical_line:
                    processed_url_content = f"{base_url}/proxy/hls/manifest.m3u8?api_password={api_password}&d={logical_line}"
                elif '.mpd' in logical_line:
                    processed_url_content = f"{base_url}/proxy/mpd/manifest.m3u8?api_password={api_password}&d={logical_line}"
                elif '.php' in logical_line:
                    processed_url_content = f"{base_url}/proxy/hls/manifest.m3u8?api_password={api_password}&d={logical_line}"
                else:
                    # Link non modificato dalle regole, ma gli header potrebbero essere aggiunti
                    pass
            else:
                # --- LOGICA SENZA PASSWORD ---
                # Non modificare link pluto.tv anche senza password
                if 'pluto.tv' in logical_line:
                    processed_url_content = logical_line
                else:
                    processed_url_content = f"{base_url}/proxy/m3u?url={logical_line}"
            
            # Applica gli header raccolti, indipendentemente dalla modalità
            if current_ext_headers:
                header_params_str = "".join([f"&h_{urllib.parse.quote(key)}={urllib.parse.quote(urllib.parse.quote(value))}" for key, value in current_ext_headers.items()])
                processed_url_content += header_params_str
                current_ext_headers = {}
            
            yield processed_url_content + '\n'
        else:
            yield line_with_newline


async def async_download_m3u_playlist(url: str) -> list[str]:
    """Scarica una playlist M3U in modo asincrono e restituisce le righe."""
    headers = {
        'User-Agent': settings.user_agent,
        'Accept': '*/*',
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept-Encoding': 'gzip, deflate',
        'Connection': 'keep-alive'
    }
    lines = []
    try:
        async with httpx.AsyncClient(verify=False, timeout=30) as client:
            async with client.stream('GET', url, headers=headers) as response:
                response.raise_for_status()
                async for line_bytes in response.aiter_lines():
                    if isinstance(line_bytes, bytes):
                        decoded_line = line_bytes.decode('utf-8', errors='replace')
                    else:
                        decoded_line = str(line_bytes)
                    lines.append(decoded_line + '\n' if decoded_line else '')
    except Exception as e:
        print(f"Errore download (async) della playlist: {str(e)}")
        raise
    return lines

async def async_generate_combined_playlist(playlist_definitions: list[str], base_url: str, api_password: Optional[str]):
    """Genera una playlist combinata da multiple definizioni, scaricando in parallelo."""
    # Prepara gli URL
    playlist_urls = []
    for definition in playlist_definitions:
        if '&' in definition:
            parts = definition.split('&', 1)
            playlist_url_str = parts[1] if len(parts) > 1 else parts[0]
        else:
            playlist_url_str = definition
        playlist_urls.append(playlist_url_str)

    # Scarica tutte le playlist in parallelo
    results = await asyncio.gather(*[async_download_m3u_playlist(url) for url in playlist_urls], return_exceptions=True)

    first_playlist_header_handled = False
    for idx, (definition, lines) in enumerate(zip(playlist_definitions, results)):
        if isinstance(lines, Exception):
            yield f"# ERROR processing playlist {playlist_urls[idx]}: {str(lines)}\n"
            continue
        playlist_lines: list[str] = lines  # type: ignore
        current_playlist_had_lines = False
        first_line_of_this_segment = True
        lines_processed_for_current_playlist = 0
        rewritten_lines_iter = rewrite_m3u_links_streaming(iter(playlist_lines), base_url, api_password)
        for line in rewritten_lines_iter:
            current_playlist_had_lines = True
            is_extm3u_line = line.strip().startswith('#EXTM3U')
            lines_processed_for_current_playlist += 1
            if not first_playlist_header_handled:
                yield line
                if is_extm3u_line:
                    first_playlist_header_handled = True
            else:
                if first_line_of_this_segment and is_extm3u_line:
                    pass
                else:
                    yield line
            first_line_of_this_segment = False
        if current_playlist_had_lines and not first_playlist_header_handled:
            first_playlist_header_handled = True


@playlist_builder_router.get("/playlist")
async def proxy_handler(
    request: Request,
    d: str = Query(..., description="Query string con le definizioni delle playlist", alias="d"),
    api_password: Optional[str] = Query(None, description="Password API per MFP"),
):
    """
    Endpoint per il proxy delle playlist M3U con supporto MFP.
    
    Formato query string: playlist1&url1;playlist2&url2
    Esempio: https://mfp.com:pass123&http://provider.com/playlist.m3u
    """
    try:
        if not d:
            raise HTTPException(status_code=400, detail="Query string mancante")

        playlist_definitions = d.split(';')
        
        # Estrai base_url dalla prima definizione se presente
        base_url = str(request.base_url).rstrip('/')
        if playlist_definitions and '&' in playlist_definitions[0]:
            parts = playlist_definitions[0].split('&', 1)
            if ':' in parts[0] and not parts[0].startswith('http'):
                # Estrai base_url dalla prima parte se contiene password
                base_url_part = parts[0].rsplit(':', 1)[0]
                if base_url_part.startswith('http'):
                    base_url = base_url_part

        async def generate_response():
            async for line in async_generate_combined_playlist(playlist_definitions, base_url, api_password):
                yield line

        return StreamingResponse(
            generate_response(),
            media_type='application/vnd.apple.mpegurl',
            headers={
                'Content-Disposition': 'attachment; filename="playlist.m3u"',
                'Access-Control-Allow-Origin': '*'
            }
        )
        
    except Exception as e:
        print(f"ERRORE GENERALE: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Errore: {str(e)}")


@playlist_builder_router.get("/builder")
async def url_builder():
    """
    Pagina con un'interfaccia per generare l'URL del proxy MFP.
    """
    html_content = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>MFP Playlist Builder</title>
        <style>
            body { font-family: Arial, sans-serif; max-width: 1200px; margin: 0 auto; padding: 20px; background-color: #f5f5f5; }
            .container { background: white; padding: 30px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
            h1 { color: #333; text-align: center; margin-bottom: 30px; }
            h2 { color: #2c5aa0; border-bottom: 2px solid #2c5aa0; padding-bottom: 5px; text-align: left; margin-top: 30px; }
            .form-group { margin-bottom: 15px; }
            label { display: block; margin-bottom: 5px; font-weight: bold; color: #555; }
            input[type="text"], input[type="url"] { width: 100%; padding: 10px; border: 1px solid #ccc; border-radius: 4px; box-sizing: border-box; }
            .btn { display: inline-block; padding: 10px 20px; background: #2c5aa0; color: white; text-decoration: none; border-radius: 5px; margin: 5px; cursor: pointer; border: none; font-size: 16px; }
            .btn:hover { background: #1e3d6f; }
            .btn-add { background-color: #28a745; }
            .btn-remove { background-color: #dc3545; padding: 5px 10px; font-size: 12px; }
            .playlist-entry { background: #f8f9fa; padding: 20px; border-radius: 5px; margin-bottom: 15px; border-left: 4px solid #17a2b8; position: relative; }
            .output-area { margin-top: 20px; }
            #generated-url { background: #e9ecef; padding: 10px; border: 1px solid #ced4da; border-radius: 4px; font-family: 'Courier New', monospace; word-break: break-all; min-height: 50px; white-space: pre-wrap; }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>🔗 MFP Playlist Builder</h1>
            
            <div class="form-group">
                <label for="server-address">MFP Server Address</label>
                <input type="text" id="server-address" placeholder="Current server address" value="" readonly style="background-color: #e9ecef;">
            </div>

            <div class="form-group">
                <label for="api-password">MFP API Password</label>
                <input type="text" id="api-password" placeholder="API password for MFP">
            </div>

            <h2>Playlists to Merge</h2>
            <div id="playlist-container">
                <!-- Playlists will be added here dynamically -->
            </div>

            <button type="button" class="btn btn-add" onclick="addPlaylistEntry()">Add Playlist</button>
            <hr style="margin: 20px 0;">

            <button type="button" class="btn" onclick="generateUrl()">Generate URL</button>

            <div class="output-area">
                <label for="generated-url">Generated URL</label>
                <div id="generated-url">The URL will appear here...</div>
                <button type="button" class="btn" onclick="copyUrl()">Copy URL</button>
            </div>
        </div>

        <!-- Template for a single playlist -->
        <template id="playlist-template">
            <div class="playlist-entry">
                <button type="button" class="btn btn-remove" style="position: absolute; top: 10px; right: 10px;" onclick="this.parentElement.remove()">Remove</button>
                <div class="form-group">
                    <label>M3U Playlist URL</label>
                    <input type="url" class="playlist-url" placeholder="Ex: http://provider.com/playlist.m3u">
                </div>
            </div>
        </template>

        <script>
            document.addEventListener('DOMContentLoaded', function() {
                // Set the default server address
                document.getElementById('server-address').value = window.location.origin;
                // Add a default playlist on startup
                addPlaylistEntry();
            });

            function addPlaylistEntry() {
                const template = document.getElementById('playlist-template').content.cloneNode(true);
                document.getElementById('playlist-container').appendChild(template);
            }

            function generateUrl() {
                const serverAddress = document.getElementById('server-address').value.trim().replace(/\\/$/, '');
                if (!serverAddress) {
                    alert('Server address not available. Please reload the page.');
                    return;
                }

                const apiPassword = document.getElementById('api-password').value.trim();
                const entries = document.querySelectorAll('.playlist-entry');
                const definitions = [];

                entries.forEach(entry => {
                    const playlistUrl = entry.querySelector('.playlist-url').value.trim();
                    if (playlistUrl) {
                        definitions.push(playlistUrl);
                    }
                });

                if (definitions.length === 0) {
                    document.getElementById('generated-url').textContent = 'No valid playlist entered.';
                    return;
                }

                let finalUrl = serverAddress + '/playlist/playlist?d=' + definitions.join(';');
                if (apiPassword) {
                    finalUrl += '&api_password=' + encodeURIComponent(apiPassword);
                }
                
                document.getElementById('generated-url').textContent = finalUrl;
            }

            function copyUrl() {
                const urlText = document.getElementById('generated-url').textContent;
                if (urlText && !urlText.startsWith('The URL') && !urlText.startsWith('No valid')) {
                    navigator.clipboard.writeText(urlText).then(() => {
                        alert('URL copied to clipboard!');
                    }).catch(err => {
                        alert('Error copying: ' + err);
                    });
                } else {
                    alert('No URL to copy.');
                }
            }
        </script>
    </body>
    </html>
    """
    return Response(content=html_content, media_type="text/html") 
