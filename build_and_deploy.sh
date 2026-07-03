#!/bin/bash
set -e

# Scriptin bulunduğu klasöre geçiş yap
cd "$(dirname "$0")"

echo "==========================================="
echo "PDF Cut Auto - Toplu Derleme ve Dağıtım"
echo "==========================================="

mkdir -p build_logs

# 1. Önceki sürüm ve ara derleme çıktılarını temizleme
echo -e "\n[1/7] Eski Derleme Çıktıları Temizleniyor..."
rm -rf dist portable_dist .nuitka-cache
mkdir -p offline_releases
find offline_releases -maxdepth 1 -type f \
    \( -name 'PDF-Kesim-Offline-*.zip' -o -name 'PDF-Kesim-Offline-*.tar.gz' \) \
    -delete
echo "✓ Eski paketler ve ara derleme çıktıları temizlendi."

# 2. Kaynak değişikliklerini sürüm kaydına alma
echo -e "\n[2/7] Değişiklikler Git'e Kaydediliyor..."
git add -u
if git diff --cached --quiet; then
    echo "✓ Commit edilecek yeni değişiklik yok."
else
    VERSION_LABEL="$(tr -d '[:space:]' < VERSION)"
    if git commit -m "Release ${VERSION_LABEL}" 2>&1 | tee build_logs/git_commit.log; then
        echo "✓ Değişiklikler commit edildi."
    else
        echo "✗ Git commit başarısız oldu! Günlük dosyasını kontrol edin: build_logs/git_commit.log"
        exit 1
    fi
fi

# 3. Windows Paketi Derleme
echo -e "\n[3/7] Windows Paketi Derleniyor..."
if .venv/bin/python build_portable.py windows-x64 2>&1 | tee build_logs/build_windows.log; then
    echo "✓ Windows derlemesi başarılı."
else
    echo "✗ Windows derlemesi başarısız oldu! Günlük dosyasını kontrol edin: build_logs/build_windows.log"
    exit 1
fi

# 4. macOS Paketi Derleme
echo -e "\n[4/7] macOS Paketi Derleniyor..."
if .venv/bin/python build_desktop.py 2>&1 | tee build_logs/build_macos.log; then
    echo "✓ macOS derlemesi başarılı."
else
    echo "✗ macOS derlemesi başarısız oldu! Günlük dosyasını kontrol edin: build_logs/build_macos.log"
    exit 1
fi

# 5. Linux Paketi Derleme
echo -e "\n[5/7] Linux Paketi Derleniyor..."
if .venv/bin/python build_portable.py linux-x64 2>&1 | tee build_logs/build_linux.log; then
    echo "✓ Linux derlemesi başarılı."
else
    echo "✗ Linux derlemesi başarısız oldu! Günlük dosyasını kontrol edin: build_logs/build_linux.log"
    exit 1
fi

# 6. Sunucuya Gönderme (Deploy)
echo -e "\n[6/7] Sunucuya Gönderiliyor (Deploy)..."
if bash deploy/deploy_vps.sh 2>&1 | tee build_logs/deploy.log; then
    echo "✓ Sunucu dağıtımı başarıyla tamamlandı!"
else
    echo "✗ Sunucu dağıtımı başarısız oldu! Günlük dosyasını kontrol edin: build_logs/deploy.log"
    exit 1
fi

# 7. Commit edilmiş sürümü GitHub'a gönderme
echo -e "\n[7/7] GitHub'a Gönderiliyor (Git Push)..."
if git push 2>&1 | tee build_logs/git_push.log; then
    echo "✓ Git push başarıyla tamamlandı."
else
    echo "✗ Git push başarısız oldu! Günlük dosyasını kontrol edin: build_logs/git_push.log"
    exit 1
fi

# Sunucuya gönderilen arşivler kalsın; yalnızca tekrar üretilebilen büyük ara
# klasörleri kaldır. Sonraki çalıştırmada bağımlılıkları yeniden indirmemek için
# .mamba-build-cache korunur.
rm -rf dist portable_dist .nuitka-cache
echo "✓ Yerel ara derleme dosyaları temizlendi."

echo -e "\n==========================================="
echo "TEBRİKLER! Tüm derlemeler ve dağıtım başarılı."
echo "==========================================="
