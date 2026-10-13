import os
import base64
import struct
import tempfile
import array
import math
import asyncio
import time
from fastapi import Request
from fastapi.responses import JSONResponse

from gemini_webapi.proxy.logging import logger, log_request_to_console

STT_REFUSAL_PATTERNS = [
    "não consigo ouvir",
    "não consigo acessar",
    "não tenho acesso ao áudio",
    "por favor, certifique-se",
    "enviar o texto por outro meio",
    "i cannot hear",
    "i can't hear",
    "cannot access the audio",
    "can't access the audio",
    "unable to access the audio",
    "file upload failed",
    "tente enviar o texto"
]


def pcm_to_wav(pcm_bytes: bytes, sample_rate: int = 16000, channels: int = 1, bits_per_sample: int = 16) -> bytes:
    """
    Prepends a 44-byte WAV header to convert raw PCM audio (LINEAR16) to a valid WAV file.
    """
    num_samples = len(pcm_bytes) // (bits_per_sample // 8)
    byte_rate = sample_rate * channels * (bits_per_sample // 8)
    block_align = channels * (bits_per_sample // 8)

    header = struct.pack('<4sI4s4sIHHIIHH4sI',
        b'RIFF',
        36 + len(pcm_bytes),
        b'WAVE',
        b'fmt ',
        16,
        1,  # Linear PCM format
        channels,
        sample_rate,
        byte_rate,
        block_align,
        bits_per_sample,
        b'data',
        len(pcm_bytes)
    )
    return header + pcm_bytes


def is_audio_silent(pcm_bytes: bytes, threshold: float = 150.0) -> bool:
    """
    Checks if 16-bit linear PCM audio is silent by calculating its RMS value.
    A threshold around 100-200 is typical for noise-floor/silence.
    """
    if len(pcm_bytes) < 2:
        return True

    try:
        shorts = array.array('h', pcm_bytes)
        sum_sq = sum(float(x) * x for x in shorts)
        rms = math.sqrt(sum_sq / len(shorts))
        logger.info(f"Calculated audio RMS: {rms:.2f} (Threshold: {threshold})")
        return rms < threshold
    except Exception as e:
        logger.warning(f"Error calculating audio RMS, assuming not silent: {e}")
        return False


async def handle_speech_recognition(request: Request):
    """
    Handles requests in Google Speech-to-Text API format.
    Converts base64 LINEAR16 audio to a temporary WAV and transcribes it via Gemini.
    """
    start_time = time.time()
    logger.info("New Speech-to-Text (STT) request received.")
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid request body."})

    audio_b64 = data.get("audio", {}).get("content", "")
    if not audio_b64:
        return JSONResponse(status_code=400, content={"error": "Audio data not found in audio.content property."})

    config = data.get("config", {})
    sample_rate = config.get("sampleRateHertz", 16000)

    try:
        # Decodifica base64 e empacota em WAV
        pcm_bytes = base64.b64decode(audio_b64)

        # Clients like Pluely send a full WAV file instead of raw PCM: unwrap it
        if pcm_bytes[:4] == b'RIFF' and pcm_bytes[8:12] == b'WAVE':
            data_pos = pcm_bytes.find(b'data', 12)
            if data_pos != -1:
                import struct
                sample_rate = struct.unpack('<I', pcm_bytes[24:28])[0] or sample_rate
                pcm_bytes = pcm_bytes[data_pos + 8:]

        # 1. Verificar se o áudio é muito curto
        if len(pcm_bytes) < 3200:  # 0.1 segundo a 16kHz
            logger.warning("Received audio is too short.")
            return JSONResponse(status_code=400, content={"error": "Audio is too short to be transcribed."})

        # 2. Verificar se o áudio está silencioso
        if is_audio_silent(pcm_bytes):
            logger.warning("Silence detected in audio.")
            return JSONResponse(status_code=400, content={"error": "No speech detected (silent audio)."})

        wav_bytes = pcm_to_wav(pcm_bytes, sample_rate=sample_rate)
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": f"Failed to process audio data: {str(e)}"})

    # Import dynamically from app to avoid circular dependencies
    from gemini_webapi.proxy.app import load_all_accounts, execute_gemini_request_with_retry, get_active_model, get_active_profile

    if not load_all_accounts():
        return JSONResponse(status_code=500, content={"error": "No account or authentication cookie configured in profiles/ folder."})

    temp_path = None
    try:
        # Writes WAV bytes to temporary file for upload
        with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as temp_file:
            temp_file.write(wav_bytes)
            temp_path = temp_file.name
            logger.info(f"STT audio saved to temp: source='audio.content [PCM {len(pcm_bytes)} bytes -> WAV {len(wav_bytes)} bytes]' -> temp_path='{temp_path}'")  

        prompt = "Transcribe this audio exactly as spoken, without adding extra comments or punctuation."

        response_text, _ = await execute_gemini_request_with_retry(
            prompt=prompt,
            files=[temp_path],
            model=get_active_model()
        )

    except asyncio.TimeoutError:
        err_msg = "Timeout exceeded during audio transcription."
        log_request_to_console(
            req_type="Speech-to-Text (STT)",
            model=get_active_model(),
            profile=get_active_profile(),
            account_info=None,
            prompt="Transcrição de Áudio",
            response_text=err_msg,
            status_code=504,
            elapsed_time=time.time() - start_time
        )
        return JSONResponse(status_code=504, content={"error": err_msg})
    except Exception as e:
        err_msg = f"Error in transcription service: {str(e)}"
        log_request_to_console(
            req_type="Speech-to-Text (STT)",
            model=get_active_model(),
            profile=get_active_profile(),
            account_info=None,
            prompt="Transcrição de Áudio",
            response_text=err_msg,
            status_code=502,
            elapsed_time=time.time() - start_time
        )
        return JSONResponse(status_code=502, content={"error": err_msg})
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass

    response_text_clean = response_text.strip()
    response_text_lower = response_text_clean.lower()

    # 3. Verificar se o texto retornado é uma recusa de áudio
    if any(pattern in response_text_lower for pattern in STT_REFUSAL_PATTERNS):
        logger.error(f"Gemini failed to transcribe audio and returned refusal: {response_text_clean}")
        return JSONResponse(
            status_code=422,
            content={
                "error": "The model could not process this audio. Please try sending text or check your microphone.",
                "details": response_text_clean
            }
        )

    # Retorna o JSON no padrão da Google Speech API
    log_request_to_console(
        req_type="Speech-to-Text (STT)",
        model=get_active_model(),
        profile=get_active_profile(),
        account_info=None,
        prompt="Transcrição de Áudio",
        response_text=response_text_clean,
        status_code=200,
        elapsed_time=time.time() - start_time
    )
    return JSONResponse(content={
        "results": [
            {
                "alternatives": [
                    {
                        "transcript": response_text_clean,
                        "confidence": 1.0
                    }
                ]
            }
        ]
    })
