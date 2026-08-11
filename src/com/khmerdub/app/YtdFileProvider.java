package com.khmerdub.app;

import android.content.ContentProvider;
import android.content.ContentValues;
import android.database.Cursor;
import android.net.Uri;
import android.os.ParcelFileDescriptor;

import java.io.File;
import java.io.FileNotFoundException;

public class YtdFileProvider extends ContentProvider {

    @Override
    public boolean onCreate() {
        return true;
    }

    @Override
    public ParcelFileDescriptor openFile(Uri uri, String mode) throws FileNotFoundException {
        File file = new File(uri.getPath());
        if (!file.exists()) {
            throw new FileNotFoundException("File not found: " + uri.getPath());
        }
        return ParcelFileDescriptor.open(file, ParcelFileDescriptor.MODE_READ_ONLY);
    }

    @Override
    public String getType(Uri uri) {
        String path = uri.getPath();
        if (path != null) {
            if (path.endsWith(".apk")) {
                return "application/vnd.android.package-archive";
            } else if (path.endsWith(".mp3")) {
                return "audio/mpeg";
            }
        }
        return "video/mp4";
    }

    @Override
    public Cursor query(Uri uri, String[] projection, String selection, String[] selectionArgs, String sortOrder) {
        try {
            File file = new File(uri.getPath());
            if (!file.exists()) return null;

            String[] cols = projection != null ? projection : new String[]{
                    android.provider.OpenableColumns.DISPLAY_NAME,
                    android.provider.OpenableColumns.SIZE
            };
            android.database.MatrixCursor cursor = new android.database.MatrixCursor(cols);
            Object[] row = new Object[cols.length];
            for (int i = 0; i < cols.length; i++) {
                if (android.provider.OpenableColumns.DISPLAY_NAME.equals(cols[i])) {
                    row[i] = file.getName();
                } else if (android.provider.OpenableColumns.SIZE.equals(cols[i])) {
                    row[i] = file.length();
                } else {
                    row[i] = null;
                }
            }
            cursor.addRow(row);
            return cursor;
        } catch (Exception e) {
            e.printStackTrace();
            return null;
        }
    }

    @Override
    public Uri insert(Uri uri, ContentValues values) {
        return null;
    }

    @Override
    public int delete(Uri uri, String selection, String[] selectionArgs) {
        return 0;
    }

    @Override
    public int update(Uri uri, ContentValues values, String selection, String[] selectionArgs) {
        return 0;
    }
}
