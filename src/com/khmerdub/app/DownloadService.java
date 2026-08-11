package com.khmerdub.app;

import android.content.Context;
import android.content.Intent;
import android.media.MediaScannerConnection;
import android.net.Uri;
import android.os.Environment;
import android.webkit.MimeTypeMap;
import android.widget.Toast;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

public class DownloadService {

    private Context context;
    private ExecutorService executor = Executors.newFixedThreadPool(4);
    private File downloadDir;

    public interface DownloadListener {
        void onProgress(String downloadId, int progress, long bytesDownloaded, long totalBytes, String status);
        void onComplete(String downloadId, String filePath);
        void onError(String downloadId, String error);
    }

    public DownloadService(Context context) {
        this.context = context;
        downloadDir = new File(Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS), "YTD");
        if (!downloadDir.exists()) {
            downloadDir.mkdirs();
        }
    }

    public void startDownload(final String downloadId, final String videoUrl, final String title, final String format, final DownloadListener listener) {
        startDownload(downloadId, videoUrl, title, format, videoUrl, listener);
    }

    public void startDownload(final String downloadId, final String videoUrl, final String title, final String format, final String pageUrl, final DownloadListener listener) {
        executor.execute(new Runnable() {
            @Override
            public void run() {
                try {
                    String sanitizedTitle = title.replaceAll("[^a-zA-Z0-9._-]", "_");
                    if (sanitizedTitle.length() > 50) sanitizedTitle = sanitizedTitle.substring(0, 50);
                    
                    String ext = "mp4";
                    if ("apk".equalsIgnoreCase(format) || title.toLowerCase().endsWith(".apk") || videoUrl.toLowerCase().contains(".apk")) {
                        ext = "apk";
                    } else if ("mp3".equalsIgnoreCase(format) || title.toLowerCase().contains("mp3")) {
                        ext = "mp3";
                    } else if (videoUrl.contains(".m3u8")) {
                        ext = "m3u8";
                    }

                    File outputFile = new File(downloadDir, sanitizedTitle + "_" + System.currentTimeMillis() + "." + ext);

                    if (videoUrl.startsWith("http://") || videoUrl.startsWith("https://")) {
                        boolean success = false;
                        if (!"apk".equalsIgnoreCase(ext)) {
                            success = downloadWithYtDlp(downloadId, videoUrl, pageUrl, outputFile, listener);
                        }
                        if (!success) {
                            downloadWithURLConnection(downloadId, videoUrl, outputFile, listener);
                        }
                    } else {
                        listener.onError(downloadId, "Invalid media URL scheme");
                    }
                } catch (Exception e) {
                    listener.onError(downloadId, e.getMessage());
                }
            }
        });
    }

    private boolean downloadWithYtDlp(String downloadId, String videoUrl, String pageUrl, File outputFile, DownloadListener listener) {
        try {
            String targetUrl = (pageUrl != null && (pageUrl.contains("youtube.com") || pageUrl.contains("youtu.be") || pageUrl.contains("facebook.com") || pageUrl.contains("tiktok.com") || pageUrl.contains("instagram.com") || pageUrl.contains("twitter.com") || pageUrl.contains("x.com"))) ? pageUrl : videoUrl;

            List<String> cmd = new ArrayList<>();
            cmd.add("python3");
            cmd.add("-m");
            cmd.add("yt_dlp");
            cmd.add("--no-warnings");
            cmd.add("-f");
            cmd.add("best[ext=mp4]/bestvideo+bestaudio/best");
            cmd.add("-S");
            cmd.add("vcodec:h264,res,acodec:m4a");
            cmd.add("-o");
            cmd.add(outputFile.getAbsolutePath());
            cmd.add(targetUrl);

            ProcessBuilder pb = new ProcessBuilder(cmd);
            pb.redirectErrorStream(true);
            Process process = pb.start();

            BufferedReader reader = new BufferedReader(new InputStreamReader(process.getInputStream()));
            String line;
            Pattern progressPattern = Pattern.compile("\\[download\\]\\s+([0-9.]+)%");
            long lastNotify = 0;

            while ((line = reader.readLine()) != null) {
                Matcher m = progressPattern.matcher(line);
                if (m.find()) {
                    float pct = Float.parseFloat(m.group(1));
                    int progress = (int) pct;
                    long now = System.currentTimeMillis();
                    if (now - lastNotify > 300) {
                        lastNotify = now;
                        listener.onProgress(downloadId, progress, (long)(progress * 1000000L), 100000000L, "Downloading Video");
                    }
                }
            }

            int exitCode = process.waitFor();
            if (exitCode == 0 && outputFile.exists() && outputFile.length() > 50000) {
                MediaScannerConnection.scanFile(context, new String[]{outputFile.getAbsolutePath()}, null, null);
                listener.onComplete(downloadId, outputFile.getAbsolutePath());
                return true;
            }
        } catch (Exception e) {
            e.printStackTrace();
        }
        return false;
    }

    private void downloadWithURLConnection(String downloadId, String videoUrl, File outputFile, DownloadListener listener) {
        InputStream input = null;
        FileOutputStream output = null;
        HttpURLConnection connection = null;

        try {
            URL url = new URL(videoUrl);
            connection = (HttpURLConnection) url.openConnection();
            connection.setConnectTimeout(15000);
            connection.setReadTimeout(30000);
            connection.setInstanceFollowRedirects(true);
            connection.setRequestProperty("User-Agent", "Mozilla/5.0 (Linux; Android 10; Mobile) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36");
            connection.setRequestProperty("Accept", "*/*");
            connection.setRequestProperty("Connection", "keep-alive");
            connection.connect();

            int responseCode = connection.getResponseCode();
            int redirectCount = 0;
            while ((responseCode == HttpURLConnection.HTTP_MOVED_TEMP || responseCode == HttpURLConnection.HTTP_MOVED_PERM || responseCode == HttpURLConnection.HTTP_SEE_OTHER || responseCode == 307 || responseCode == 308) && redirectCount < 10) {
                String newUrl = connection.getHeaderField("Location");
                if (newUrl == null || newUrl.isEmpty()) break;
                connection.disconnect();
                url = new URL(newUrl);
                connection = (HttpURLConnection) url.openConnection();
                connection.setConnectTimeout(15000);
                connection.setReadTimeout(30000);
                connection.setInstanceFollowRedirects(true);
                connection.setRequestProperty("User-Agent", "Mozilla/5.0 (Linux; Android 10; Mobile) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36");
                connection.setRequestProperty("Accept", "*/*");
                connection.connect();
                responseCode = connection.getResponseCode();
                redirectCount++;
            }

            String contentType = connection.getContentType();
            if (contentType != null && contentType.toLowerCase().contains("text/html")) {
                throw new Exception("HTML page returned instead of video stream. Use Web Sniffer to download stream.");
            }

            int fileLength = connection.getContentLength();
            input = connection.getInputStream();
            output = new FileOutputStream(outputFile);

            byte[] buffer = new byte[16384];
            long total = 0;
            int count;
            long lastUpdate = System.currentTimeMillis();

            while ((count = input.read(buffer)) != -1) {
                total += count;
                output.write(buffer, 0, count);

                long now = System.currentTimeMillis();
                if (now - lastUpdate > 300) {
                    lastUpdate = now;
                    int progress = fileLength > 0 ? (int) (total * 100 / fileLength) : 0;
                    listener.onProgress(downloadId, progress, total, fileLength, "Downloading");
                }
            }

            output.flush();
            output.close();
            input.close();

            if (fileLength > 0 && total < fileLength) {
                outputFile.delete();
                throw new Exception("Download incomplete: received " + formatFileSize(total) + " of " + formatFileSize(fileLength));
            }

            long actualSize = outputFile.length();
            if (actualSize < 1000000 && !outputFile.getName().endsWith(".apk") && !outputFile.getName().endsWith(".mp3")) {
                outputFile.delete();
                throw new Exception("Stream restricted or invalid file size (" + formatFileSize(actualSize) + ")");
            }

            MediaScannerConnection.scanFile(context, new String[]{outputFile.getAbsolutePath()}, null, null);
            listener.onComplete(downloadId, outputFile.getAbsolutePath());
        } catch (Exception e) {
            e.printStackTrace();
            if (outputFile.exists() && outputFile.length() < 2000000 && !outputFile.getName().endsWith(".apk")) {
                outputFile.delete();
            }
            String msg = e.getMessage();
            if (msg == null) msg = "Unknown connection error";
            if (msg.startsWith("Download failed: ")) msg = msg.substring(17);
            listener.onError(downloadId, msg);
        } finally {
            try {
                if (output != null) output.close();
                if (input != null) input.close();
            } catch (Exception ignored) {}
            if (connection != null) connection.disconnect();
        }
    }

    public JSONArray getDownloadedFiles() {
        JSONArray array = new JSONArray();
        try {
            if (downloadDir != null && downloadDir.exists()) {
                File[] files = downloadDir.listFiles();
                if (files != null) {
                    for (File file : files) {
                        if (file.isFile()) {
                            JSONObject item = new JSONObject();
                            item.put("name", file.getName());
                            item.put("path", file.getAbsolutePath());
                            item.put("size", formatFileSize(file.length()));
                            item.put("lastModified", file.lastModified());
                            item.put("isAudio", file.getName().endsWith(".mp3"));
                            array.put(item);
                        }
                    }
                }
            }
        } catch (Exception e) {
            e.printStackTrace();
        }
        return array;
    }

    public boolean deleteFile(String path) {
        try {
            File f = new File(path);
            if (f.exists()) {
                return f.delete();
            }
        } catch (Exception e) {
            e.printStackTrace();
        }
        return false;
    }

    public void openFile(String path) {
        try {
            File file = new File(path);
            if (!file.exists()) {
                Toast.makeText(context, "File does not exist: " + path, Toast.LENGTH_SHORT).show();
                return;
            }

            if (path.endsWith(".apk")) {
                installApk(path);
                return;
            }

            Uri contentUri = Uri.parse("content://com.khmerdub.app.fileprovider" + file.getAbsolutePath());
            String mimeType = file.getName().toLowerCase().endsWith(".mp3") ? "audio/*" : "video/*";

            Intent intent = new Intent(Intent.ACTION_VIEW);
            intent.setDataAndType(contentUri, mimeType);
            intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_ACTIVITY_NEW_TASK);
            context.startActivity(intent);
        } catch (Exception e) {
            e.printStackTrace();
            Toast.makeText(context, "Cannot open file externally: " + e.getMessage(), Toast.LENGTH_SHORT).show();
        }
    }

    public void installApk(String path) {
        try {
            File file = new File(path);
            if (!file.exists()) {
                Toast.makeText(context, "APK file not found: " + path, Toast.LENGTH_SHORT).show();
                return;
            }

            if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.O) {
                if (!context.getPackageManager().canRequestPackageInstalls()) {
                    Toast.makeText(context, "Please enable unknown app sources permission for KhmerDub Pro", Toast.LENGTH_LONG).show();
                    Intent settingsIntent = new Intent(android.provider.Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES);
                    settingsIntent.setData(Uri.parse("package:" + context.getPackageName()));
                    settingsIntent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                    context.startActivity(settingsIntent);
                    return;
                }
            }

            Uri apkUri = Uri.parse("content://com.khmerdub.app.fileprovider" + file.getAbsolutePath());
            Intent intent = new Intent(Intent.ACTION_VIEW);
            intent.setDataAndType(apkUri, "application/vnd.android.package-archive");
            intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_ACTIVITY_NEW_TASK);
            context.startActivity(intent);
        } catch (Exception e) {
            e.printStackTrace();
            Toast.makeText(context, "Failed to launch installer: " + e.getMessage(), Toast.LENGTH_SHORT).show();
        }
    }

    private String formatFileSize(long size) {
        if (size <= 0) return "0 B";
        final String[] units = new String[]{"B", "KB", "MB", "GB", "TB"};
        int digitGroups = (int) (Math.log10(size) / Math.log10(1024));
        return String.format("%.1f %s", size / Math.pow(1024, digitGroups), units[digitGroups]);
    }
}
